package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

type Format struct {
	Result struct {
		ID      string `json:"id"`
		Choices []struct {
			FinishReason       string      `json:"finish_reason"`
			Index              int         `json:"index"`
			Logprobs           interface{} `json:"logprobs"`
			Message            interface{} `json:"message"`
			NativeFinishReason string      `json:"native_finish_reason"`
		} `json:"choices"`
		Created           int         `json:"created"`
		Model             string      `json:"model"`
		Object            string      `json:"object"`
		ServiceTier       interface{} `json:"service_tier"`
		SystemFingerprint string      `json:"system_fingerprint"`
		Usage             interface{} `json:"usage"`
		Provider          string      `json:"provider"`
	} `json:"result"`
	Input struct {
		Messages   interface{} `json:"messages"`
		Parameters interface{} `json:"parameters"`
	} `json:"input"`
	Key string `json:"key"`
}

var validFinishReasons = map[string]bool{
	"stop":           true,
	"length":         true,
	"tool_calls":     true,
	"content_filter": true,
	"function_call":  true,
}

func checkFile(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("failed to read file: %w", err)
	}

	var format Format
	if err := json.Unmarshal(data, &format); err != nil {
		return fmt.Errorf("failed to parse JSON: %w", err)
	}

	// Check if Choices exists and has at least one element
	if len(format.Result.Choices) == 0 {
		return fmt.Errorf("no choices found")
	}

	// Check FinishReason in Choices[0]
	finishReason := format.Result.Choices[0].FinishReason
	if !validFinishReasons[finishReason] {
		return fmt.Errorf("invalid finish_reason: '%s' (must be one of: stop, length, tool_calls, content_filter, function_call)", finishReason)
	}

	return nil
}

func worker(jobs <-chan string, results chan<- string, wg *sync.WaitGroup) {
	defer wg.Done()
	for path := range jobs {
		if err := checkFile(path); err != nil {
			results <- fmt.Sprintf("%s: %v", path, err)
		}
	}
}

func main() {
	// Get current directory
	root, err := os.Getwd()
	if err != nil {
		fmt.Printf("Error getting current directory: %v\n", err)
		os.Exit(1)
	}

	// Collect all files
	var files []string
	err = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		// Skip directories and the check_format.go file itself
		if !info.IsDir() && filepath.Base(path) != "check_format.go" {
			files = append(files, path)
		}
		return nil
	})
	if err != nil {
		fmt.Printf("Error walking directory: %v\n", err)
		os.Exit(1)
	}

	// Create channels
	jobs := make(chan string, len(files))
	results := make(chan string, len(files))

	// Start 4 worker goroutines
	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go worker(jobs, results, &wg)
	}

	// Send all files to jobs channel
	for _, file := range files {
		jobs <- file
	}
	close(jobs)

	// Wait for all workers to finish and close results
	go func() {
		wg.Wait()
		close(results)
	}()

	// Print all errors
	errorCount := 0
	for result := range results {
		fmt.Println(result)
		errorCount++
	}

	if errorCount > 0 {
		fmt.Printf("\nTotal files with errors: %d\n", errorCount)
		os.Exit(1)
	} else {
		fmt.Println("All files passed validation!")
	}
}
