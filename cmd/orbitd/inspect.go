package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

func inspectCommand(args []string, token string, out io.Writer) error {
	opts := flag.NewFlagSet("orbitd inspect", flag.ContinueOnError)
	addr := opts.String("listen", defaultListenAddr, "address of the local orbitd HTTP API")
	opts.Usage = func() {
		fmt.Fprintln(opts.Output(), "Usage: orbitd inspect [--listen ADDR] TURN_ID")
		opts.PrintDefaults()
	}
	if err := opts.Parse(args); err != nil {
		return err
	}
	if opts.NArg() != 1 || !identifier.MatchString(opts.Arg(0)) {
		return errors.New("provide one Turn ID (usage: orbitd inspect [--listen ADDR] TURN_ID)")
	}
	if !isLoopbackAddress(*addr) {
		return errors.New("listen address must be a numeric loopback IP and port")
	}
	if len(token) < 16 {
		return errors.New("set ORBITD_TOKEN to the same token used to start orbitd")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	client := &http.Client{
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	path := "http://" + *addr + "/turns/" + opts.Arg(0)
	var current turn
	if err := inspectGet(ctx, client, path, token, &current); err != nil {
		return err
	}
	last, lastTool, err := inspectEvents(ctx, client, path+"/events", token)
	if err != nil {
		return err
	}
	return describeTurn(out, current, last, lastTool)
}

func inspectGet(ctx context.Context, client *http.Client, endpoint, token string, into any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+token)
	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("contact orbitd: %w", err)
	}
	defer response.Body.Close()
	switch response.StatusCode {
	case http.StatusOK:
		return json.NewDecoder(response.Body).Decode(into)
	case http.StatusUnauthorized:
		return errors.New("orbitd rejected ORBITD_TOKEN")
	case http.StatusNotFound:
		return errors.New("turn not found")
	default:
		return fmt.Errorf("orbitd returned HTTP %d", response.StatusCode)
	}
}

func inspectEvents(ctx context.Context, client *http.Client, endpoint, token string) (event, event, error) {
	var last, lastTool event
	for {
		var page []event
		cursor := last.Seq
		if err := inspectGet(ctx, client, endpoint+"?after="+strconv.FormatInt(cursor, 10), token, &page); err != nil {
			return event{}, event{}, err
		}
		for _, item := range page {
			last = item
			if item.Kind == "tool" {
				lastTool = item
			}
		}
		if len(page) < eventPageSize {
			return last, lastTool, nil
		}
		if last.Seq <= cursor {
			return event{}, event{}, errors.New("orbitd event cursor did not advance")
		}
	}
}

func describeTurn(out io.Writer, current turn, last, lastTool event) error {
	if terminal(last.Kind) {
		// The events lookup may observe completion after the Turn lookup.
		current.Status = last.Kind
	}
	var report strings.Builder
	fmt.Fprintf(&report, "Turn: %s\nWorkspace: %s\nConversation: %s\nStatus: %s\n",
		current.ID, current.Workspace, current.Conversation, current.Status)
	if last.Seq != 0 {
		fmt.Fprintf(&report, "Last event: #%d %s at %s\n", last.Seq, last.Kind, last.ObservedAt)
		var detail struct {
			Reason string `json:"reason"`
		}
		if json.Unmarshal(last.Data, &detail) == nil && detail.Reason != "" {
			fmt.Fprintf(&report, "Reason: %s\n", detail.Reason)
		}
	}
	if lastTool.Seq == 0 {
		fmt.Fprintln(&report, "Last tool: no persisted tool event")
	} else {
		var tool struct {
			Name   string `json:"name"`
			Phase  string `json:"phase"`
			Failed bool   `json:"failed"`
		}
		if err := json.Unmarshal(lastTool.Data, &tool); err != nil {
			return fmt.Errorf("decode tool event #%d: %w", lastTool.Seq, err)
		}
		fmt.Fprintf(&report, "Last tool: #%d %s %s", lastTool.Seq, tool.Name, tool.Phase)
		if tool.Phase == "complete" {
			if tool.Failed {
				fmt.Fprint(&report, " (failed)")
			} else {
				fmt.Fprint(&report, " (succeeded)")
			}
		}
		fmt.Fprintln(&report)
	}
	if current.Status == "unknown" {
		fmt.Fprintln(&report, "Review the workspace side effects before deciding whether to submit again.")
	}
	_, err := io.WriteString(out, report.String())
	return err
}
