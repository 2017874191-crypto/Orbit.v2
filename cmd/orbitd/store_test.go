package main

import (
	"errors"
	"sync"
	"testing"
)

func TestAdmissionSerializesDuplicateSubmissions(t *testing.T) {
	s, err := openStore(t.TempDir(), map[string]string{"alpha": "alpha", "beta": "beta"})
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	req := submission{Workspace: "alpha", Conversation: "demo", RequestKey: "key", Text: "demo"}
	var calls sync.WaitGroup
	ids := make(chan string, 20)
	for range cap(ids) {
		calls.Add(1)
		go func() {
			defer calls.Done()
			current, _, err := s.admit(req, 1)
			if err != nil {
				t.Error(err)
				return
			}
			ids <- current.ID
		}()
	}
	calls.Wait()
	close(ids)
	first := <-ids
	if first == "" {
		t.Fatal("no turn admitted")
	}
	for id := range ids {
		if id != first {
			t.Fatalf("received duplicate turn %s, want %s", id, first)
		}
	}
	req.Workspace = "beta"
	if _, _, err := s.admit(req, 1); !errors.Is(err, errBusy) {
		t.Fatalf("admitted work beyond the global limit: %v", err)
	}
}

func TestRestartPreservesAcceptedAndMarksDispatchingUnknown(t *testing.T) {
	root := t.TempDir()
	workspaces := map[string]string{"alpha": "alpha", "beta": "beta"}
	s, err := openStore(root, workspaces)
	if err != nil {
		t.Fatal(err)
	}
	a, _, err := s.admit(submission{Workspace: "alpha", Conversation: "c", RequestKey: "a", Text: "demo"}, 2)
	if err != nil {
		t.Fatal(err)
	}
	b, _, err := s.admit(submission{Workspace: "beta", Conversation: "c", RequestKey: "b", Text: "demo"}, 2)
	if err != nil {
		t.Fatal(err)
	}
	if changed, err := s.record(a.ID, "dispatching", nil); err != nil || !changed {
		t.Fatalf("persist dispatch: changed=%v, err=%v", changed, err)
	}
	if err := s.db.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = openStore(root, workspaces)
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	if got, err := s.get(a.ID); err != nil || got.Status != "unknown" {
		t.Fatalf("dispatch recovery: %+v, %v", got, err)
	}
	if got, err := s.next("beta"); err != nil || got.ID != b.ID {
		t.Fatalf("accepted recovery: %+v, %v", got, err)
	}
	if got, err := s.events(a.ID, 0); err != nil || len(got) != 3 || got[2].Kind != "unknown" {
		t.Fatalf("persisted terminal event: %+v, %v", got, err)
	}
}
