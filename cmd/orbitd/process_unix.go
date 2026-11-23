//go:build !windows

package main

import (
	"os/exec"
	"syscall"
)

type processOwner struct{ pid int }

func startOwned(cmd *exec.Cmd) (processOwner, error) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		return processOwner{}, err
	}
	return processOwner{pid: cmd.Process.Pid}, nil
}

func (owner processOwner) kill()  { _ = syscall.Kill(-owner.pid, syscall.SIGKILL) }
func (owner processOwner) close() {}
