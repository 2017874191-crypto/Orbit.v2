package main

import (
	"bufio"
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"
)

const protocolVersion = 1

type command struct {
	Kind         string `json:"kind"`
	ID           string `json:"turn_id"`
	Conversation string `json:"conversation_id,omitempty"`
	Text         string `json:"text,omitempty"`
}

type workerFrame struct {
	Kind    string          `json:"kind"`
	Version int             `json:"version"`
	PID     int64           `json:"pid"`
	ID      string          `json:"turn_id"`
	Data    json.RawMessage `json:"data"`
}

type worker struct {
	cmd     *exec.Cmd
	owner   processOwner
	input   io.WriteCloser
	frames  chan workerFrame
	discard chan struct{}
	done    chan struct{}
}

func launchWorker(python, workspace, state, config string) (*worker, error) {
	cmd := exec.Command(python, "-u", "-m", "orbit.worker", "--workspace", workspace, "--state", state, "--config", config)
	for _, variable := range os.Environ() {
		key, _, _ := strings.Cut(variable, "=")
		if !strings.EqualFold(key, "ORBITD_TOKEN") && !strings.EqualFold(key, "ORBIT_HOME") {
			cmd.Env = append(cmd.Env, variable)
		}
	}
	cmd.Env = append(cmd.Env, "ORBIT_HOME="+state)
	cmd.Stderr = os.Stderr
	input, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	output, err := cmd.StdoutPipe()
	if err != nil {
		_ = input.Close()
		return nil, err
	}
	owner, err := startOwned(cmd)
	if err != nil {
		_ = input.Close()
		_ = output.Close()
		return nil, err
	}
	w := &worker{cmd: cmd, owner: owner, input: input, frames: make(chan workerFrame), discard: make(chan struct{}), done: make(chan struct{})}
	go w.read(output)
	return w, nil
}

func (w *worker) read(output io.ReadCloser) {
	defer close(w.done)
	defer w.cmd.Wait()
	defer close(w.frames)
	defer output.Close()
	scanner := bufio.NewScanner(output)
	scanner.Buffer(make([]byte, 4096), 1<<20)
	scanner.Split(func(data []byte, atEOF bool) (int, []byte, error) {
		if atEOF && len(data) != 0 && !bytes.ContainsRune(data, '\n') {
			return 0, nil, io.ErrUnexpectedEOF
		}
		return bufio.ScanLines(data, atEOF)
	})
	for scanner.Scan() {
		var frame workerFrame
		if err := json.Unmarshal(scanner.Bytes(), &frame); err != nil {
			log.Printf("worker pid=%d sent an invalid frame", w.cmd.Process.Pid)
			break
		}
		select {
		case w.frames <- frame:
		case <-w.discard:
			return
		}
	}
	if err := scanner.Err(); err != nil {
		log.Printf("worker pid=%d protocol read: %v", w.cmd.Process.Pid, err)
	}
}

func (w *worker) send(req command) error {
	result := make(chan error, 1)
	go func() { result <- json.NewEncoder(w.input).Encode(req) }()
	timer := time.NewTimer(3 * time.Second)
	defer timer.Stop()
	select {
	case err := <-result:
		return err
	case <-timer.C:
		w.owner.kill()
		return errors.New("worker pipe write timed out")
	}
}

func (w *worker) stop() {
	defer w.owner.close()
	close(w.discard)
	_ = w.input.Close()
	timer := time.NewTimer(2 * time.Second)
	defer timer.Stop()
	select {
	case <-w.done:
	case <-timer.C:
		w.owner.kill()
		<-w.done
	}
}

type workspaceWorker struct {
	id   string
	path string
	wake chan struct{}
	pid  atomic.Int64
}

func (workspace *workspaceWorker) signal() {
	select {
	case workspace.wake <- struct{}{}:
	default:
	}
}

func supervise(ctx context.Context, s *store, workspace *workspaceWorker, python, root, config string) error {
	state := filepath.Join(root, "workers", "workspace-"+workspace.id)
	for ctx.Err() == nil {
		w, err := launchWorker(python, workspace.path, state, config)
		if err == nil {
			err = serveWorker(ctx, s, workspace, w)
			if err != nil {
				return err
			}
		} else {
			log.Printf("workspace %s start: %v", workspace.id, err)
		}
		timer := time.NewTimer(time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
		case <-timer.C:
		}
	}
	return nil
}

func serveWorker(ctx context.Context, s *store, workspace *workspaceWorker, w *worker) (err error) {
	var current string
	exitReason := `{"reason":"worker_exited"}`
	defer func() {
		workspace.pid.Store(0)
		w.stop()
		if err == nil {
			err = recordUnknown(s, current, exitReason)
		}
	}()
	startup := time.NewTimer(30 * time.Second)
	defer startup.Stop()
	select {
	case frame, open := <-w.frames:
		if !open || frame.Kind != "ready" || frame.Version != protocolVersion || frame.PID <= 0 {
			log.Printf("workspace %s worker exited or failed protocol handshake", workspace.id)
			return nil
		}
		workspace.pid.Store(frame.PID)
	case <-startup.C:
		log.Printf("workspace %s worker readiness timed out", workspace.id)
		return nil
	case <-ctx.Done():
		return nil
	}
	log.Printf("workspace %s ready pid=%d", workspace.id, workspace.pid.Load())
	workspace.signal()
	var cancelSent bool
	stopSignal := ctx.Done()
	var deadline <-chan time.Time
	defer func() { log.Printf("workspace %s worker stopped pid=%d", workspace.id, w.cmd.Process.Pid) }()
	for {
		select {
		case <-stopSignal:
			stopSignal = nil
			_ = w.input.Close()
			timer := time.NewTimer(4 * time.Second)
			defer timer.Stop()
			deadline = timer.C
		case <-deadline:
			deadline = nil
			w.owner.kill()
		case <-workspace.wake:
			if ctx.Err() != nil {
				continue
			}
			if current == "" {
				t, err := s.next(workspace.id)
				if errors.Is(err, sql.ErrNoRows) {
					continue
				}
				if err != nil {
					return err
				}
				changed, err := s.record(t.ID, "dispatching", nil)
				if err != nil {
					return err
				}
				if !changed { // The caller cancelled before the durable dispatch boundary.
					workspace.signal()
					continue
				}
				current, cancelSent = t.ID, false
				if err := w.send(command{Kind: "submit", ID: t.ID, Conversation: t.Conversation, Text: t.Text}); err != nil {
					log.Printf("dispatch %s: %v", t.ID, err)
					w.owner.kill()
				}
			} else if !cancelSent {
				t, err := s.get(current)
				if err != nil {
					return err
				}
				if t.Cancel {
					cancelSent = true
					if err := w.send(command{Kind: "cancel", ID: current}); err != nil {
						log.Printf("cancel %s: %v", current, err)
						w.owner.kill()
					}
				}
			}
		case frame, open := <-w.frames:
			if !open {
				return nil
			}
			if current == "" || current != frame.ID || !validWorkerEvent(frame.Kind) {
				exitReason = `{"reason":"invalid_worker_event"}`
				return nil
			}
			changed, err := s.record(current, frame.Kind, frame.Data)
			if err != nil {
				return fmt.Errorf("persist worker event: %w", err)
			}
			if !changed {
				exitReason = `{"reason":"invalid_worker_event_order"}`
				return nil
			}
			if terminal(frame.Kind) {
				current = ""
				workspace.signal()
			}
		}
	}
}

func recordUnknown(s *store, id, reason string) error {
	if id == "" {
		return nil
	}
	_, err := s.record(id, "unknown", json.RawMessage(reason))
	return err
}

func validWorkerEvent(kind string) bool {
	return terminal(kind) || kind == "running" || kind == "tool" || kind == "reply"
}

func terminal(status string) bool {
	switch status {
	case "completed", "failed", "cancelled", "unknown":
		return true
	default:
		return false
	}
}
