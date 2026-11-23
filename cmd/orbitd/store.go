package main

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"time"

	_ "modernc.org/sqlite"
)

const (
	activeStates  = "('accepted','dispatching','running')"
	eventPageSize = 200
)

var (
	errBusy     = errors.New("concurrency limit reached")
	errConflict = errors.New("request key already refers to different content")
)

type submission struct {
	Workspace    string `json:"workspace"`
	Conversation string `json:"conversation_id"`
	RequestKey   string `json:"request_key"`
	Text         string `json:"text"`
}

type turn struct {
	ID           string `json:"turn_id"`
	Workspace    string `json:"workspace"`
	Status       string `json:"status"`
	Cancel       bool   `json:"cancel_requested"`
	Conversation string `json:"conversation_id"`
	Text         string `json:"-"`
}

type event struct {
	Seq        int64           `json:"seq"`
	Kind       string          `json:"kind"`
	ObservedAt string          `json:"observed_at"`
	Data       json.RawMessage `json:"data,omitempty"`
}

type store struct{ db *sql.DB }

func openStore(root string, workspaces map[string]string) (*store, error) {
	db, err := sql.Open("sqlite", filepath.Join(root, "turns.sqlite"))
	if err != nil {
		return nil, err
	}
	// One daemon owns this database. A single connection serializes all admission decisions.
	db.SetMaxOpenConns(1)
	_, err = db.Exec(`
		PRAGMA foreign_keys = ON;
		CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, path TEXT NOT NULL);
		CREATE TABLE IF NOT EXISTS turns (
			id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id),
			request_key TEXT NOT NULL, conversation TEXT NOT NULL, prompt TEXT NOT NULL,
			status TEXT NOT NULL CHECK (status IN ('accepted','dispatching','running','completed','failed','cancelled','unknown')),
			cancel_requested INTEGER NOT NULL DEFAULT 0, UNIQUE(workspace, request_key)
		);
		CREATE INDEX IF NOT EXISTS turns_admission ON turns(workspace,status);
		CREATE TABLE IF NOT EXISTS events (
			seq INTEGER PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
			kind TEXT NOT NULL, data TEXT, observed_at TEXT NOT NULL
		);
		CREATE INDEX IF NOT EXISTS events_cursor ON events(turn_id,seq);
	`)
	s := &store{db: db}
	if err == nil {
		err = s.registerWorkspaces(workspaces)
	}
	if err == nil {
		err = s.recover()
	}
	if err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *store) registerWorkspaces(workspaces map[string]string) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	rows, err := tx.Query(`SELECT id,path FROM workspaces`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id, path string
		if err := rows.Scan(&id, &path); err != nil {
			return err
		}
		if workspaces[id] != path {
			return fmt.Errorf("workspace %q must keep its registered path in this state directory", id)
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for id, path := range workspaces {
		if _, err := tx.Exec(`INSERT OR IGNORE INTO workspaces VALUES (?,?)`, id, path); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *store) recover() error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.Exec(`INSERT INTO events(turn_id, kind, data, observed_at)
		SELECT id,'unknown','{"reason":"manager_restarted"}',? FROM turns WHERE status IN ('dispatching','running')`,
		time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
		return err
	}
	if _, err := tx.Exec(`UPDATE turns SET status='unknown' WHERE status IN ('dispatching','running')`); err != nil {
		return err
	}
	return tx.Commit()
}

func scanTurn(row *sql.Row) (turn, error) {
	var t turn
	err := row.Scan(&t.ID, &t.Workspace, &t.Status, &t.Cancel, &t.Conversation, &t.Text)
	return t, err
}

const turnSelect = `SELECT id,workspace,status,cancel_requested,conversation,prompt FROM turns`

func (s *store) admit(req submission, limit int) (turn, bool, error) {
	tx, err := s.db.Begin()
	if err != nil {
		return turn{}, false, err
	}
	defer tx.Rollback()
	previous, err := scanTurn(tx.QueryRow(turnSelect+` WHERE workspace=? AND request_key=?`, req.Workspace, req.RequestKey))
	if err == nil {
		if previous.Text != req.Text || previous.Conversation != req.Conversation {
			return turn{}, false, errConflict
		}
		return previous, true, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return turn{}, false, err
	}
	var active, inWorkspace int
	if err := tx.QueryRow(`SELECT count(*),count(CASE WHEN workspace=? THEN 1 END) FROM turns WHERE status IN `+activeStates,
		req.Workspace).Scan(&active, &inWorkspace); err != nil {
		return turn{}, false, err
	}
	if active >= limit || inWorkspace > 0 {
		return turn{}, false, errBusy
	}
	var randomID [16]byte
	if _, err := rand.Read(randomID[:]); err != nil {
		return turn{}, false, err
	}
	id := hex.EncodeToString(randomID[:])
	if _, err := tx.Exec(`INSERT INTO turns(id,workspace,request_key,conversation,prompt,status) VALUES (?,?,?,?,?,'accepted')`,
		id, req.Workspace, req.RequestKey, req.Conversation, req.Text); err != nil {
		return turn{}, false, err
	}
	if err := appendEvent(tx, id, "accepted", nil); err != nil {
		return turn{}, false, err
	}
	return turn{ID: id, Workspace: req.Workspace, Conversation: req.Conversation, Status: "accepted"}, false, tx.Commit()
}

func appendEvent(tx *sql.Tx, id, kind string, data json.RawMessage) error {
	_, err := tx.Exec(`INSERT INTO events(turn_id,kind,data,observed_at) VALUES (?,?,NULLIF(?,''),?)`,
		id, kind, string(data), time.Now().UTC().Format(time.RFC3339Nano))
	return err
}

func (s *store) get(id string) (turn, error) {
	return scanTurn(s.db.QueryRow(turnSelect+` WHERE id=?`, id))
}

func (s *store) next(workspace string) (turn, error) {
	return scanTurn(s.db.QueryRow(turnSelect+` WHERE workspace=? AND status='accepted'`, workspace))
}

func (s *store) cancel(id string) (turn, error) {
	tx, err := s.db.Begin()
	if err != nil {
		return turn{}, err
	}
	defer tx.Rollback()
	t, err := scanTurn(tx.QueryRow(turnSelect+` WHERE id=?`, id))
	if err != nil {
		return t, err
	}
	var kind string
	switch t.Status {
	case "accepted":
		t.Status, t.Cancel, kind = "cancelled", true, "cancelled"
	case "dispatching", "running":
		if !t.Cancel {
			t.Cancel, kind = true, "cancel_requested"
		}
	}
	if kind != "" {
		if _, err := tx.Exec(`UPDATE turns SET status=?,cancel_requested=? WHERE id=?`, t.Status, t.Cancel, id); err != nil {
			return t, err
		}
		if err := appendEvent(tx, id, kind, nil); err != nil {
			return t, err
		}
	}
	return t, tx.Commit()
}

func (s *store) record(id, kind string, data json.RawMessage) (bool, error) {
	var from, to string
	switch kind {
	case "dispatching":
		from, to = "('accepted')", kind
	case "running":
		from, to = "('dispatching')", kind
	case "tool", "reply":
		from = "('running')"
	case "completed":
		from, to = "('running')", kind
	case "failed", "cancelled", "unknown":
		from, to = "('dispatching','running')", kind
	default:
		return false, fmt.Errorf("unrecognized event %q", kind)
	}
	tx, err := s.db.Begin()
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	result, err := tx.Exec(`UPDATE turns SET status=COALESCE(NULLIF(?,''),status) WHERE id=? AND status IN `+from, to, id)
	if err != nil {
		return false, err
	}
	n, err := result.RowsAffected()
	if err != nil || n == 0 {
		return false, err
	}
	if err := appendEvent(tx, id, kind, data); err != nil {
		return false, err
	}
	return true, tx.Commit()
}

func (s *store) events(id string, after int64) ([]event, error) {
	rows, err := s.db.Query(`SELECT seq,kind,observed_at,COALESCE(data,'') FROM events WHERE turn_id=? AND seq>? ORDER BY seq LIMIT ?`, id, after, eventPageSize)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]event, 0)
	for rows.Next() {
		var e event
		var data string
		if err := rows.Scan(&e.Seq, &e.Kind, &e.ObservedAt, &data); err != nil {
			return nil, err
		}
		e.Data = json.RawMessage(data)
		items = append(items, e)
	}
	return items, rows.Err()
}
