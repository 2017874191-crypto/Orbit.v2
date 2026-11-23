package main

import (
	"database/sql"
	"encoding/json"
	"log"
	"time"
)

// benchmarkTimings correlates writes and reads on one monotonic clock.
// It is nil during regular daemon runs.
type benchmarkTimings struct{ origin time.Time }

type benchmarkSample struct {
	TurnID        string `json:"turn_id"`
	Kind          string `json:"kind"`
	Phase         string `json:"phase"`
	AtNS          int64  `json:"at_ns"`
	TransactionNS int64  `json:"transaction_ns,omitempty"`
	CommitNS      int64  `json:"commit_ns,omitempty"`
}

func (b *benchmarkTimings) startTransaction() time.Time {
	if b == nil {
		return time.Time{}
	}
	return time.Now()
}

func (b *benchmarkTimings) commit(tx *sql.Tx, began time.Time, id, kind string) error {
	if b == nil {
		return tx.Commit()
	}
	commitStart := time.Now()
	if err := tx.Commit(); err != nil {
		return err
	}
	finished := time.Now()
	b.log(benchmarkSample{
		TurnID: id, Kind: kind, Phase: "commit", AtNS: finished.Sub(b.origin).Nanoseconds(),
		TransactionNS: finished.Sub(began).Nanoseconds(), CommitNS: finished.Sub(commitStart).Nanoseconds(),
	})
	return nil
}

func (b *benchmarkTimings) eventsReady(id string, items []event) {
	if b == nil || len(items) == 0 || items[len(items)-1].Kind != "completed" {
		return
	}
	b.log(benchmarkSample{TurnID: id, Kind: "completed", Phase: "events_ready", AtNS: time.Since(b.origin).Nanoseconds()})
}

func (b *benchmarkTimings) log(sample benchmarkSample) {
	data, _ := json.Marshal(sample)
	log.Printf("orbitd-benchmark %s", data)
}
