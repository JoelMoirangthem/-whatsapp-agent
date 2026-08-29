package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/purpshell/meowcaller"
	"go.mau.fi/whatsmeow"
)

// Active call registry
type activeCallEntry struct {
	call      *meowcaller.Call
	recipient string
	callType  string
	startedAt time.Time
}

var (
	callMu       sync.Mutex
	activeCalls  = make(map[string]*activeCallEntry)
	callerClient *meowcaller.Client
	callerOnce   sync.Once
)

func getCallerClient(wa *whatsmeow.Client) *meowcaller.Client {
	if wa == nil {
		return nil
	}
	callerOnce.Do(func() {
		callerClient = meowcaller.NewClient(wa)
	})
	return callerClient
}

// CallRequest is the JSON payload for POST /api/call
type CallRequest struct {
	Recipient string `json:"recipient"`
	IsVideo   bool   `json:"is_video"`
}

// CallResponse is the JSON response for /api/call
type CallResponse struct {
	Success   bool   `json:"success"`
	CallID    string `json:"call_id,omitempty"`
	Recipient string `json:"recipient,omitempty"`
	CallType  string `json:"call_type,omitempty"`
	Status    string `json:"status,omitempty"`
	Message   string `json:"message,omitempty"`
	Error     string `json:"error,omitempty"`
}

// CallHangupRequest is the JSON payload for POST /api/call/hangup
type CallHangupRequest struct {
	CallID string `json:"call_id"`
}

// registerCallEndpoints wires /api/call and /api/call/hangup into the REST router
func registerCallEndpoints(mux *http.ServeMux, auth func(http.HandlerFunc) http.HandlerFunc, client *whatsmeow.Client, messageStore *MessageStore) {
	mux.HandleFunc("/api/call", auth(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		w.Header().Set("Content-Type", "application/json")

		var req CallRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			_ = json.NewEncoder(w).Encode(CallResponse{Success: false, Error: "Invalid JSON request format"})
			return
		}

		target := strings.TrimSpace(req.Recipient)
		if target == "" {
			w.WriteHeader(http.StatusBadRequest)
			_ = json.NewEncoder(w).Encode(CallResponse{Success: false, Error: "recipient is required"})
			return
		}

		if client == nil || !client.IsConnected() {
			w.WriteHeader(http.StatusServiceUnavailable)
			_ = json.NewEncoder(w).Encode(CallResponse{Success: false, Error: "WhatsApp client is not connected"})
			return
		}

		caller := getCallerClient(client)
		if caller == nil {
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(CallResponse{Success: false, Error: "Calling engine not initialized"})
			return
		}

		ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
		defer cancel()

		callType := "audio"
		if req.IsVideo {
			callType = "video"
		}

		call, err := caller.CallWithOptions(ctx, target, meowcaller.CallOptions{Video: req.IsVideo})
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(CallResponse{
				Success: false,
				Error:   fmt.Sprintf("Failed to place WhatsApp %s call: %v", callType, err),
			})
			return
		}

		callID := call.ID()
		callMu.Lock()
		activeCalls[callID] = &activeCallEntry{
			call:      call,
			recipient: target,
			callType:  callType,
			startedAt: time.Now(),
		}
		callMu.Unlock()

		// Record in message store if available
		if messageStore != nil {
			var fromJID string
			if client.Store != nil && client.Store.ID != nil {
				fromJID = client.Store.ID.ToNonAD().String()
			}
			_ = messageStore.StoreCallOffer(callID, target, fromJID, time.Now(), true, callType, false)
		}

		// Handle lifecycle cleanup
		call.OnEnd(func(reason string) {
			callMu.Lock()
			_, exists := activeCalls[callID]
			delete(activeCalls, callID)
			callMu.Unlock()

			if exists && messageStore != nil {
				_ = messageStore.MarkCallTerminated(callID, target, reason, time.Now())
			}
			fmt.Printf("WhatsApp %s call %s ended: reason=%s\n", callType, callID, reason)
		})

		_ = json.NewEncoder(w).Encode(CallResponse{
			Success:   true,
			CallID:    callID,
			Recipient: target,
			CallType:  callType,
			Status:    "calling",
			Message:   fmt.Sprintf("WhatsApp %s call initiated to %s", callType, target),
		})
	}))

	mux.HandleFunc("/api/call/hangup", auth(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		w.Header().Set("Content-Type", "application/json")

		var req CallHangupRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			_ = json.NewEncoder(w).Encode(CallResponse{Success: false, Error: "Invalid JSON request format"})
			return
		}

		callMu.Lock()
		entry, exists := activeCalls[req.CallID]
		callMu.Unlock()

		if !exists || entry.call == nil {
			w.WriteHeader(http.StatusNotFound)
			_ = json.NewEncoder(w).Encode(CallResponse{Success: false, Error: "Active call not found"})
			return
		}

		if err := entry.call.Hangup(); err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(CallResponse{Success: false, Error: fmt.Sprintf("Failed to hang up call: %v", err)})
			return
		}

		_ = json.NewEncoder(w).Encode(CallResponse{
			Success: true,
			CallID:  req.CallID,
			Message: "Call hung up successfully",
		})
	}))
}
