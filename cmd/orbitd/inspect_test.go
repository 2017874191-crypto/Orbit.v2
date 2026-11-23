package main

import (
	"encoding/json"
	"io"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestInspectRequiresNumericLoopbackAddress(t *testing.T) {
	for _, address := range []string{"192.0.2.1:9062", "127.0.0.1:", "127.0.0.1:http", "127.0.0.1:65536"} {
		t.Run(address, func(t *testing.T) {
			err := inspectCommand([]string{"--listen", address, "turn-id"}, "test-inspect-token", io.Discard)
			if err == nil || err.Error() != "listen address must be a numeric loopback IP and port" {
				t.Fatalf("expected local address validation, got %v", err)
			}
		})
	}
}

func TestInspectFindsEventsAfterFirstPage(t *testing.T) {
	s, err := openStore(t.TempDir(), map[string]string{"alpha": "alpha"})
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	current, _, err := s.admit(submission{Workspace: "alpha", Conversation: "demo", RequestKey: "key", Text: "edit"}, 1)
	if err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"dispatching", "running"} {
		if changed, err := s.record(current.ID, kind, nil); err != nil || !changed {
			t.Fatalf("record %s: changed=%v, err=%v", kind, changed, err)
		}
	}
	tx, err := s.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for i := 3; i < eventPageSize; i++ {
		if err := appendEvent(tx, current.ID, "tool", json.RawMessage(`{"name":"read_file","phase":"start"}`)); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	for _, item := range []struct{ kind, data string }{
		{"tool", `{"name":"edit_file","phase":"complete","failed":false}`},
		{"unknown", `{"reason":"manager_restarted"}`},
	} {
		if changed, err := s.record(current.ID, item.kind, json.RawMessage(item.data)); err != nil || !changed {
			t.Fatalf("record %s: changed=%v, err=%v", item.kind, changed, err)
		}
	}
	const token = "test-inspect-token"
	server := httptest.NewServer((&manager{store: s, token: token}).routes())
	defer server.Close()
	args := []string{"--listen", strings.TrimPrefix(server.URL, "http://"), current.ID}
	var output strings.Builder
	if err := inspectCommand(args, token, &output); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"Turn: " + current.ID,
		"Status: unknown",
		"Last event: #202 unknown",
		"Reason: manager_restarted",
		"Last tool: #201 edit_file complete (succeeded)",
	} {
		if !strings.Contains(output.String(), want) {
			t.Errorf("missing %q in output:\n%s", want, output.String())
		}
	}
}
