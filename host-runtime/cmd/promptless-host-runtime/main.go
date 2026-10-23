package main

import (
	hostruntime "github.com/Promptless/pig-toolchain/host-runtime/internal/runtime"
	"os"
)

func main() { os.Exit(hostruntime.Main(os.Args[1:], os.Stdin, os.Stdout, os.Stderr)) }
