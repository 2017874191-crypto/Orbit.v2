// orbitd supervises a local Orbit process per configured workspace.
package main

import (
	"context"
	"crypto/subtle"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gofrs/flock"
)

var identifier = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`)

type workspaceFlags map[string]string

func (f workspaceFlags) String() string { return fmt.Sprint(map[string]string(f)) }

func (f workspaceFlags) Set(value string) error {
	id, path, found := strings.Cut(value, "=")
	if !found || !identifier.MatchString(id) || path == "" {
		return errors.New("workspace must be id=directory")
	}
	for other := range f {
		if strings.EqualFold(other, id) {
			return fmt.Errorf("duplicate workspace ID %q", id)
		}
	}
	f[id] = path
	return nil
}

type manager struct {
	store      *store
	workspaces map[string]*workspaceWorker
	limit      int
	token      string
}

func (m *manager) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", m.health)
	mux.HandleFunc("POST /turns", m.submit)
	mux.HandleFunc("GET /turns/{id}", m.get)
	mux.HandleFunc("GET /turns/{id}/events", m.events)
	mux.HandleFunc("POST /turns/{id}/cancel", m.cancel)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+m.token)) != 1 {
			respond(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
			return
		}
		mux.ServeHTTP(w, r)
	})
}

func (m *manager) health(w http.ResponseWriter, _ *http.Request) {
	workers := make(map[string]int64, len(m.workspaces))
	for id, target := range m.workspaces {
		workers[id] = target.pid.Load() // Zero means that the worker is starting or unavailable.
	}
	respond(w, http.StatusOK, struct {
		Workers map[string]int64 `json:"workers"`
		Limit   int              `json:"concurrency"`
	}{Workers: workers, Limit: m.limit})
}

func (m *manager) submit(w http.ResponseWriter, r *http.Request) {
	var req submission
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 96<<10))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&req) != nil || decoder.Decode(new(any)) != io.EOF ||
		!identifier.MatchString(req.Conversation) || strings.TrimSpace(req.RequestKey) == "" ||
		len(req.RequestKey) > 128 || strings.TrimSpace(req.Text) == "" || len(req.Text) > 16<<10 {
		respond(w, http.StatusBadRequest, map[string]string{"error": "invalid_submission"})
		return
	}
	target, ok := m.workspaces[req.Workspace]
	if !ok {
		respond(w, http.StatusNotFound, map[string]string{"error": "workspace_not_found"})
		return
	}
	t, replay, err := m.store.admit(req, m.limit)
	switch {
	case errors.Is(err, errBusy):
		w.Header().Set("Retry-After", "1")
		respond(w, http.StatusTooManyRequests, map[string]string{"error": "busy"})
	case errors.Is(err, errConflict):
		respond(w, http.StatusConflict, map[string]string{"error": "request_key_conflict"})
	case err != nil:
		managerError(w, err)
	case replay:
		respond(w, http.StatusOK, t)
	default:
		target.signal()
		respond(w, http.StatusAccepted, t)
	}
}

func (m *manager) get(w http.ResponseWriter, r *http.Request) {
	t, err := m.store.get(r.PathValue("id"))
	if err != nil {
		managerError(w, err)
		return
	}
	respond(w, http.StatusOK, t)
}

func (m *manager) events(w http.ResponseWriter, r *http.Request) {
	var after int64
	if value := r.URL.Query().Get("after"); value != "" {
		cursor, err := strconv.ParseInt(value, 10, 64)
		if err != nil || cursor < 0 {
			respond(w, http.StatusBadRequest, map[string]string{"error": "invalid_cursor"})
			return
		}
		after = cursor
	}
	id := r.PathValue("id")
	if _, err := m.store.get(id); err != nil {
		managerError(w, err)
		return
	}
	items, err := m.store.events(id, after)
	if err != nil {
		managerError(w, err)
		return
	}
	respond(w, http.StatusOK, items)
}

func (m *manager) cancel(w http.ResponseWriter, r *http.Request) {
	t, err := m.store.cancel(r.PathValue("id"))
	if err != nil {
		managerError(w, err)
		return
	}
	m.workspaces[t.Workspace].signal()
	respond(w, http.StatusOK, t)
}

func managerError(w http.ResponseWriter, err error) {
	if errors.Is(err, sql.ErrNoRows) {
		respond(w, http.StatusNotFound, map[string]string{"error": "turn_not_found"})
		return
	}
	log.Printf("store: %v", err)
	respond(w, http.StatusInternalServerError, map[string]string{"error": "store_error"})
}

func respond(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(body)
}

func absolute(path string) (string, error) {
	path, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(path)
}

func containsPath(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}

func run(ctx context.Context, addr, root, python, config, token string, paths workspaceFlags, limit int) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		return errors.New("listen address must be a numeric loopback IP and port")
	}
	if len(paths) == 0 || root == "" || limit < 1 || len(token) < 16 {
		return errors.New("provide --state, at least one --workspace, positive --concurrency and ORBITD_TOKEN (min 16 bytes)")
	}
	if err := os.MkdirAll(root, 0700); err != nil {
		return err
	}
	root, err = absolute(root)
	if err != nil {
		return err
	}
	if config == "" {
		config = filepath.Join(root, "config.json")
	}
	config, err = absolute(config)
	if err != nil {
		return fmt.Errorf("resolve Orbit config: %w", err)
	}
	if info, err := os.Stat(config); err != nil || !info.Mode().IsRegular() {
		return errors.New("Orbit config must be a regular file")
	}
	python, err = exec.LookPath(python)
	if err != nil {
		return err
	}
	m := &manager{limit: limit, token: token, workspaces: map[string]*workspaceWorker{}}
	for id, path := range paths {
		resolved, err := absolute(path)
		if err != nil {
			return fmt.Errorf("workspace %s: %w", id, err)
		}
		if info, err := os.Stat(resolved); err != nil || !info.IsDir() {
			return fmt.Errorf("workspace %s must be a directory", id)
		}
		if containsPath(resolved, root) || containsPath(root, resolved) {
			return fmt.Errorf("state directory overlaps workspace %s", id)
		}
		for _, other := range m.workspaces {
			if containsPath(other.path, resolved) || containsPath(resolved, other.path) {
				return fmt.Errorf("workspaces %s and %s overlap", id, other.id)
			}
		}
		paths[id] = resolved
		m.workspaces[id] = &workspaceWorker{id: id, path: resolved, wake: make(chan struct{}, 1)}
	}
	lock := flock.New(filepath.Join(root, "orbitd.lock"))
	locked, err := lock.TryLock()
	if err != nil {
		return err
	}
	if !locked {
		return errors.New("the state directory already has an orbitd process")
	}
	defer lock.Unlock()
	m.store, err = openStore(root, paths)
	if err != nil {
		return err
	}
	defer m.store.db.Close()
	listener, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	ctx, stop := context.WithCancel(ctx)
	defer stop()
	var workers sync.WaitGroup
	workerErrors := make(chan error, len(paths))
	for _, target := range m.workspaces {
		workers.Add(1)
		go func() {
			defer workers.Done()
			if err := supervise(ctx, m.store, target, python, root, config); err != nil {
				workerErrors <- err
			}
		}()
	}
	server := &http.Server{Handler: m.routes(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second}
	serverErrors := make(chan error, 1)
	go func() { serverErrors <- server.Serve(listener) }()
	log.Printf("orbitd listening on http://%s", listener.Addr())
	select {
	case err = <-workerErrors:
	case err = <-serverErrors:
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 12*time.Second)
	defer cancel()
	shutdownErr := server.Shutdown(shutdown)
	stop()
	workers.Wait()
	return errors.Join(err, shutdownErr)
}

func main() {
	paths := workspaceFlags{}
	flag.Var(paths, "workspace", "configured workspace as id=directory (repeatable)")
	listen := flag.String("listen", "127.0.0.1:9062", "local HTTP API address")
	root := flag.String("state", "", "dedicated orbitd state directory")
	python := flag.String("python", "python", "Python interpreter with Orbit installed")
	config := flag.String("config", "", "Orbit config path (defaults to config.json in --state)")
	limit := flag.Int("concurrency", 1, "maximum admitted turns across workspaces")
	flag.Parse()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, *listen, *root, *python, *config, os.Getenv("ORBITD_TOKEN"), paths, *limit); err != nil {
		log.Fatal(err)
	}
}
