// Package config reads service settings from the environment, so a service is
// configured the same way whether it runs in a container or on your laptop.
package config

import (
	"os"
	"strconv"
	"strings"
	"time"
)

// Str returns the environment variable key, or def when it is unset or empty.
func Str(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

// Dur parses key as a Go duration, falling back to def.
func Dur(key string, def time.Duration) time.Duration {
	v := Str(key, "")
	if v == "" {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return def
	}
	return d
}

// Int parses key as an integer, falling back to def.
func Int(key string, def int) int {
	v := Str(key, "")
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

// List splits key on commas, trimming spaces and dropping empty items.
func List(key string) []string {
	parts := strings.Split(Str(key, ""), ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
