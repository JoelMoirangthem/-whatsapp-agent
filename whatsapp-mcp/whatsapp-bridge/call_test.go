package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestCallEndpointsValidation(t *testing.T) {
	mux := http.NewServeMux()
	auth := func(h http.HandlerFunc) http.HandlerFunc {
		return h
	}
	registerCallEndpoints(mux, auth, nil, nil)

	t.Run("call invalid method", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/api/call", nil)
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)

		if rec.Code != http.StatusMethodNotAllowed {
			t.Fatalf("expected status 405, got %d", rec.Code)
		}
	})

	t.Run("call empty recipient", func(t *testing.T) {
		body, _ := json.Marshal(CallRequest{Recipient: ""})
		req := httptest.NewRequest(http.MethodPost, "/api/call", bytes.NewReader(body))
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)

		if rec.Code != http.StatusBadRequest {
			t.Fatalf("expected status 400, got %d", rec.Code)
		}
	})

	t.Run("call disconnected client", func(t *testing.T) {
		body, _ := json.Marshal(CallRequest{Recipient: "1234567890@s.whatsapp.net"})
		req := httptest.NewRequest(http.MethodPost, "/api/call", bytes.NewReader(body))
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)

		if rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("expected status 503, got %d", rec.Code)
		}
	})

	t.Run("hangup not found", func(t *testing.T) {
		body, _ := json.Marshal(CallHangupRequest{CallID: "nonexistent-call"})
		req := httptest.NewRequest(http.MethodPost, "/api/call/hangup", bytes.NewReader(body))
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)

		if rec.Code != http.StatusNotFound {
			t.Fatalf("expected status 404, got %d", rec.Code)
		}
	})
}
