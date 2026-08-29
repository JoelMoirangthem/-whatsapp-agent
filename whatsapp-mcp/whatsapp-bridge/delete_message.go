package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

// DELETE /api/messages (POST also accepted for clients that cannot send a
// body with DELETE) revokes a previously sent message — WhatsApp's "delete
// for everyone".
//
// Safety model (fail-closed):
//   - The target message must exist in the local store, so ownership can be
//     verified before any protocol message goes out. Unknown IDs are 404.
//   - Messages not sent by this account can only be revoked in groups, and
//     only when the caller passes the original sender's JID, which must match
//     the stored record. Revoking other people's messages in DMs is not
//     possible on WhatsApp and is refused here.
//   - WHATSAPP_DELETE_MAX_AGE_HOURS optionally rejects revokes of messages
//     older than the given number of hours (0 = no local limit; the WhatsApp
//     server still enforces its own window).
//
// On success the local row is stamped deleted_at immediately instead of
// waiting for the REVOKE echo, which handleMessageRevoke deduplicates via
// first-revoke-wins semantics.

// DeleteMessageRequest is the request body for the delete-message API.
type DeleteMessageRequest struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	// SenderJID is required when deleting a message authored by someone else
	// (group admin revoke). Must match the sender recorded in the store.
	SenderJID string `json:"sender_jid,omitempty"`
}

// DeleteMessageResponse is the response for the delete-message API.
type DeleteMessageResponse struct {
	Success   bool   `json:"success"`
	Message   string `json:"message"`
	MessageID string `json:"message_id,omitempty"`
}

// messageMeta is the subset of a stored message needed to authorize a revoke.
type messageMeta struct {
	Sender    string
	IsFromMe  bool
	Timestamp time.Time
}

// GetMessageMeta returns the authorization-relevant fields for one message.
// Returns (nil, nil) when the row does not exist.
func (store *MessageStore) GetMessageMeta(id, chatJID string) (*messageMeta, error) {
	var meta messageMeta
	err := store.db.QueryRow(
		"SELECT COALESCE(sender, ''), is_from_me, timestamp FROM messages WHERE id = ? AND chat_jid = ?",
		id, chatJID,
	).Scan(&meta.Sender, &meta.IsFromMe, &meta.Timestamp)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &meta, nil
}

// deleteTarget carries everything the handler needs to send and record a revoke.
type deleteTarget struct {
	persistChatJID string    // chat JID under which the row is stored locally
	sendChatJID    types.JID // chat JID to address the revoke to (LID-resolved for DMs)
	sender         types.JID // BuildRevoke sender arg; EmptyJID means "our own message"
	messageID      types.MessageID
}

// resolveDeleteTarget validates a delete request against the local store and
// returns the target to revoke. The returned status code accompanies err when
// err is non-nil. Fail-closed: any doubt results in an error.
func resolveDeleteTarget(client *whatsmeow.Client, ms *MessageStore, req *DeleteMessageRequest, maxAge time.Duration) (*deleteTarget, int, error) {
	chatJID, err := types.ParseJID(req.ChatJID)
	if err != nil || chatJID.User == "" || chatJID.Server == "" {
		return nil, http.StatusBadRequest, errors.New("invalid chat_jid")
	}

	meta := lookupMessageMeta(client, ms, req.MessageID, chatJID)
	if meta == nil {
		return nil, http.StatusNotFound, errors.New("message not found in local store; refusing to revoke unverified messages")
	}

	target := &deleteTarget{
		persistChatJID: chatJID.String(),
		sendChatJID:    chatJID,
		messageID:      types.MessageID(req.MessageID),
	}

	if meta.IsFromMe {
		if client == nil || client.Store == nil || client.Store.ID == nil {
			return nil, http.StatusServiceUnavailable, errors.New("not logged in")
		}
		// Own messages revoke with an empty sender (whatsmeow fills the key).
		target.sender = types.EmptyJID
	} else {
		// Someone else's message: group-admin revoke only, and the caller
		// must name the author so we never guess who is being revoked.
		if chatJID.Server != types.GroupServer {
			return nil, http.StatusForbidden, errors.New("only messages sent by this account can be deleted")
		}
		if strings.TrimSpace(req.SenderJID) == "" {
			return nil, http.StatusBadRequest, errors.New("sender_jid is required to delete another participant's message")
		}
		senderJID, parseErr := parseSenderJID(req.SenderJID)
		if parseErr != nil {
			return nil, http.StatusBadRequest, errors.New("invalid sender_jid")
		}
		if !sameUserAcrossJIDForms(client, senderJID.String(), meta.Sender) {
			return nil, http.StatusForbidden, errors.New("sender_jid does not match the recorded author of this message")
		}
		target.sender = senderJID
	}

	if maxAge > 0 && time.Since(meta.Timestamp) > maxAge {
		return nil, http.StatusForbidden, fmt.Errorf("message is older than the configured %s deletion window", maxAge)
	}

	// DMs are addressed via LID for migrated contacts — mirror the send path.
	if target.sendChatJID.Server == types.DefaultUserServer && client != nil && client.Store != nil && client.Store.LIDs != nil {
		if lid, lidErr := client.Store.LIDs.GetLIDForPN(context.Background(), target.sendChatJID); lidErr == nil && !lid.IsEmpty() {
			target.sendChatJID = lid
		}
	}

	return target, 0, nil
}

// lookupMessageMeta finds a row under its exact chat JID, retrying once with
// the phone-JID form for @lid chats (DM rows persist under phone JIDs).
func lookupMessageMeta(client *whatsmeow.Client, ms *MessageStore, messageID string, chatJID types.JID) *messageMeta {
	meta, err := ms.GetMessageMeta(messageID, chatJID.String())
	if err != nil || meta != nil {
		return meta
	}
	if chatJID.Server != types.HiddenUserServer {
		return nil
	}
	resolved := resolveUserJID(client, chatJID, types.EmptyJID)
	if resolved.String() == chatJID.String() {
		return nil
	}
	meta, err = ms.GetMessageMeta(messageID, resolved.String())
	if err != nil {
		return nil
	}
	return meta
}

// parseSenderJID accepts a full JID or a bare phone/LID user part.
func parseSenderJID(s string) (types.JID, error) {
	s = strings.TrimSpace(s)
	if strings.Contains(s, "@") {
		return types.ParseJID(s)
	}
	jid := types.NewJID(s, types.DefaultUserServer)
	if jid.User == "" || jid.Server == "" {
		return types.EmptyJID, errors.New("empty JID")
	}
	return jid, nil
}

// sameUserAcrossJIDForms compares two identifiers (full JIDs or bare users),
// allowing LID↔phone equivalence via the whatsmeow LID store.
func sameUserAcrossJIDForms(client *whatsmeow.Client, a, b string) bool {
	ua, ub := bareSenderUser(a), bareSenderUser(b)
	if ua == ub {
		return true
	}
	if client == nil || client.Store == nil || client.Store.LIDs == nil {
		return false
	}
	ctx := context.Background()
	lidOf := func(u string) string {
		lid, err := client.Store.LIDs.GetLIDForPN(ctx, types.NewJID(u, types.DefaultUserServer))
		if err != nil || lid.IsEmpty() {
			return ""
		}
		return lid.User
	}
	pnOf := func(u string) string {
		pn, err := client.Store.LIDs.GetPNForLID(ctx, types.NewJID(u, types.HiddenUserServer))
		if err != nil || pn.IsEmpty() {
			return ""
		}
		return pn.User
	}
	return lidOf(ua) == ub || lidOf(ub) == ua || pnOf(ua) == ub || pnOf(ub) == ua
}

// registerDeleteEndpoint wires DELETE /api/messages into the mux.
func registerDeleteEndpoint(mux *http.ServeMux, auth func(http.HandlerFunc) http.HandlerFunc, client *whatsmeow.Client, messageStore *MessageStore) {
	maxAge := deleteMaxAgeFromEnv()

	mux.HandleFunc("/api/messages", auth(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete && r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		fmt.Printf("→ /api/messages method=%s from=%q\n", r.Method, r.RemoteAddr)

		var req DeleteMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		if req.ChatJID == "" || req.MessageID == "" {
			http.Error(w, "chat_jid and message_id are required", http.StatusBadRequest)
			return
		}

		w.Header().Set("Content-Type", "application/json")

		target, status, err := resolveDeleteTarget(client, messageStore, &req, maxAge)
		if err != nil {
			w.WriteHeader(status)
			_ = json.NewEncoder(w).Encode(DeleteMessageResponse{Success: false, Message: err.Error()})
			return
		}

		if _, err := client.SendMessage(context.Background(), target.sendChatJID, client.BuildRevoke(target.sendChatJID, target.sender, target.messageID)); err != nil {
			fmt.Printf("← /api/messages success=false id=%q err=%v\n", req.MessageID, err)
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(DeleteMessageResponse{
				Success: false,
				Message: fmt.Sprintf("Error sending revoke: %v", err),
			})
			return
		}

		// Stamp the local archive immediately; the REVOKE echo is a no-op
		// thanks to first-revoke-wins in MarkMessageDeleted.
		if err := messageStore.MarkMessageDeleted(req.MessageID, target.persistChatJID, time.Now()); err != nil {
			fmt.Printf("Warning: failed to mark message %s as deleted locally: %v\n", req.MessageID, err)
		}

		fmt.Printf("← /api/messages success=true id=%q chat=%q\n", req.MessageID, target.persistChatJID)
		_ = json.NewEncoder(w).Encode(DeleteMessageResponse{
			Success:   true,
			Message:   fmt.Sprintf("Message %s deleted for everyone", req.MessageID),
			MessageID: req.MessageID,
		})
	}))
}

// deleteMaxAgeFromEnv reads WHATSAPP_DELETE_MAX_AGE_HOURS (0 or unset = off).
func deleteMaxAgeFromEnv() time.Duration {
	raw := strings.TrimSpace(os.Getenv("WHATSAPP_DELETE_MAX_AGE_HOURS"))
	if raw == "" {
		return 0
	}
	hours, err := strconv.ParseFloat(raw, 64)
	if err != nil || hours <= 0 {
		fmt.Printf("Warning: ignoring invalid WHATSAPP_DELETE_MAX_AGE_HOURS=%q\n", raw)
		return 0
	}
	return time.Duration(hours * float64(time.Hour))
}
