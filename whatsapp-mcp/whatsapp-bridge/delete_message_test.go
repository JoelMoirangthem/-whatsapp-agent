package main

import (
	"database/sql"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
)

// --- Fixtures ---

const (
	delDMChat   = "15550001111@s.whatsapp.net"
	delGroupJID = "12039125555@g.us"
	delOtherPN  = "15552224444" // bare phone of the other participant
)

// seedDeleteMessage inserts a message row for delete tests.
func seedDeleteMessage(t *testing.T, ms *MessageStore, id, chatJID, sender string, isFromMe bool, ts time.Time) {
	t.Helper()
	if _, err := ms.db.Exec(
		`INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me)
		 VALUES (?, ?, ?, ?, ?, ?)`,
		id, chatJID, sender, "hello", ts, isFromMe,
	); err != nil {
		t.Fatalf("seed message: %v", err)
	}
}

// --- GetMessageMeta ---

func TestGetMessageMeta(t *testing.T) {
	ms := newTestMessageStore(t)
	ts := time.Unix(1710000000, 0)
	seedDeleteMessage(t, ms, "m1", delDMChat, delOtherPN, false, ts)

	meta, err := ms.GetMessageMeta("m1", delDMChat)
	if err != nil || meta == nil {
		t.Fatalf("GetMessageMeta() = (%v, %v), want meta", meta, err)
	}
	if meta.Sender != delOtherPN || meta.IsFromMe || !meta.Timestamp.Equal(ts) {
		t.Fatalf("meta = %+v, want sender=%s from_me=false ts=%v", meta, delOtherPN, ts)
	}

	meta, err = ms.GetMessageMeta("missing", delDMChat)
	if meta != nil || err != nil {
		t.Fatalf("GetMessageMeta(missing) = (%v, %v), want (nil, nil)", meta, err)
	}
}

// --- resolveDeleteTarget ---

func TestResolveDeleteTarget_OwnMessage(t *testing.T) {
	ms := newTestMessageStore(t)
	client := newTestClientWithSelf(&mockLIDStore{}, phonePN)
	seedDeleteMessage(t, ms, "own1", delDMChat, phonePN.User, true, time.Now())

	target, status, err := resolveDeleteTarget(client, ms,
		&DeleteMessageRequest{ChatJID: delDMChat, MessageID: "own1"}, 0)
	if err != nil {
		t.Fatalf("unexpected error (status %d): %v", status, err)
	}
	if target.sender != types.EmptyJID {
		t.Fatalf("sender = %v, want EmptyJID for own message", target.sender)
	}
	if target.persistChatJID != delDMChat {
		t.Fatalf("persistChatJID = %q, want %q", target.persistChatJID, delDMChat)
	}
	if target.sendChatJID.String() != delDMChat {
		t.Fatalf("sendChatJID = %v, want %v (no LID mapping configured)", target.sendChatJID, delDMChat)
	}
}

func TestResolveDeleteTarget_OwnMessageLIDResolvedForSend(t *testing.T) {
	lidUser := "184125298348272"

	ms := newTestMessageStore(t)
	client := newTestClientWithSelf(&mockLIDStore{
		lidByPN: map[types.JID]types.JID{
			{User: "15550001111", Server: types.DefaultUserServer}: {User: lidUser, Server: types.HiddenUserServer},
		},
	}, phonePN)
	seedDeleteMessage(t, ms, "own2", delDMChat, phonePN.User, true, time.Now())

	target, status, err := resolveDeleteTarget(client, ms,
		&DeleteMessageRequest{ChatJID: delDMChat, MessageID: "own2"}, 0)
	if err != nil {
		t.Fatalf("unexpected error (status %d): %v", status, err)
	}
	if target.sendChatJID.Server != types.HiddenUserServer || target.sendChatJID.User != lidUser {
		t.Fatalf("sendChatJID = %v, want @lid JID with user %s", target.sendChatJID, lidUser)
	}
}

func TestResolveDeleteTarget_NotFoundFailsClosed(t *testing.T) {
	ms := newTestMessageStore(t)
	client := newTestClientWithSelf(&mockLIDStore{}, phonePN)

	_, status, err := resolveDeleteTarget(client, ms,
		&DeleteMessageRequest{ChatJID: delDMChat, MessageID: "ghost"}, 0)
	if status != http.StatusNotFound || err == nil {
		t.Fatalf("status=%d err=%v, want 404 with error", status, err)
	}
}

func TestResolveDeleteTarget_DMOthersMessageForbidden(t *testing.T) {
	ms := newTestMessageStore(t)
	client := newTestClientWithSelf(&mockLIDStore{}, phonePN)
	seedDeleteMessage(t, ms, "inb1", delDMChat, delOtherPN, false, time.Now())

	_, status, err := resolveDeleteTarget(client, ms,
		&DeleteMessageRequest{ChatJID: delDMChat, MessageID: "inb1"}, 0)
	if status != http.StatusForbidden || err == nil {
		t.Fatalf("status=%d err=%v, want 403 for DM revoke of another's message", status, err)
	}
}

func TestResolveDeleteTarget_GroupOthersMessage(t *testing.T) {
	senderFull := delOtherPN + "@s.whatsapp.net"

	t.Run("requires sender_jid", func(t *testing.T) {
		ms := newTestMessageStore(t)
		client := newTestClientWithSelf(&mockLIDStore{}, phonePN)
		seedDeleteMessage(t, ms, "g1", delGroupJID, delOtherPN, false, time.Now())

		_, status, err := resolveDeleteTarget(client, ms,
			&DeleteMessageRequest{ChatJID: delGroupJID, MessageID: "g1"}, 0)
		if status != http.StatusBadRequest || err == nil {
			t.Fatalf("status=%d err=%v, want 400 without sender_jid", status, err)
		}
	})

	t.Run("rejects mismatched sender", func(t *testing.T) {
		ms := newTestMessageStore(t)
		client := newTestClientWithSelf(&mockLIDStore{}, phonePN)
		seedDeleteMessage(t, ms, "g1", delGroupJID, delOtherPN, false, time.Now())

		_, status, err := resolveDeleteTarget(client, ms,
			&DeleteMessageRequest{ChatJID: delGroupJID, MessageID: "g1", SenderJID: "19999999999@s.whatsapp.net"}, 0)
		if status != http.StatusForbidden || err == nil {
			t.Fatalf("status=%d err=%v, want 403 on sender mismatch", status, err)
		}
	})

	t.Run("accepts matching sender", func(t *testing.T) {
		ms := newTestMessageStore(t)
		client := newTestClientWithSelf(&mockLIDStore{}, phonePN)
		seedDeleteMessage(t, ms, "g1", delGroupJID, delOtherPN, false, time.Now())

		target, status, err := resolveDeleteTarget(client, ms,
			&DeleteMessageRequest{ChatJID: delGroupJID, MessageID: "g1", SenderJID: senderFull}, 0)
		if err != nil {
			t.Fatalf("unexpected error (status %d): %v", status, err)
		}
		if target.sender.String() != senderFull {
			t.Fatalf("sender = %v, want %v", target.sender, senderFull)
		}
	})

	t.Run("matches author via LID equivalence", func(t *testing.T) {
		lidUser := "184125298348272"
		ms := newTestMessageStore(t)
		lids := &mockLIDStore{pnByLID: map[types.JID]types.JID{
			{User: lidUser, Server: types.HiddenUserServer}: {User: delOtherPN, Server: types.DefaultUserServer},
		}}
		client := newTestClientWithSelf(lids, phonePN)
		seedDeleteMessage(t, ms, "g2", delGroupJID, lidUser, false, time.Now())

		target, status, err := resolveDeleteTarget(client, ms,
			&DeleteMessageRequest{ChatJID: delGroupJID, MessageID: "g2", SenderJID: delOtherPN + "@s.whatsapp.net"}, 0)
		if err != nil {
			t.Fatalf("unexpected error (status %d): %v", status, err)
		}
		if target.sender.User != delOtherPN {
			t.Fatalf("sender user = %q, want %q", target.sender.User, delOtherPN)
		}
	})
}

func TestResolveDeleteTarget_MaxAgeWindow(t *testing.T) {
	old := time.Now().Add(-72 * time.Hour)

	t.Run("rejects beyond window", func(t *testing.T) {
		ms := newTestMessageStore(t)
		client := newTestClientWithSelf(&mockLIDStore{}, phonePN)
		seedDeleteMessage(t, ms, "old1", delDMChat, phonePN.User, true, old)

		_, status, err := resolveDeleteTarget(client, ms,
			&DeleteMessageRequest{ChatJID: delDMChat, MessageID: "old1"}, time.Hour)
		if status != http.StatusForbidden || err == nil {
			t.Fatalf("status=%d err=%v, want 403 beyond max age", status, err)
		}
	})

	t.Run("disabled window allows old messages", func(t *testing.T) {
		ms := newTestMessageStore(t)
		client := newTestClientWithSelf(&mockLIDStore{}, phonePN)
		seedDeleteMessage(t, ms, "old1", delDMChat, phonePN.User, true, old)

		_, status, err := resolveDeleteTarget(client, ms,
			&DeleteMessageRequest{ChatJID: delDMChat, MessageID: "old1"}, 0)
		if err != nil {
			t.Fatalf("unexpected error (status %d): %v", status, err)
		}
	})
}

func TestResolveDeleteTarget_NotLoggedIn(t *testing.T) {
	ms := newTestMessageStore(t)
	client := newTestClient(&mockLIDStore{}) // Store.ID unset
	seedDeleteMessage(t, ms, "own3", delDMChat, phonePN.User, true, time.Now())

	_, status, err := resolveDeleteTarget(client, ms,
		&DeleteMessageRequest{ChatJID: delDMChat, MessageID: "own3"}, 0)
	if status != http.StatusServiceUnavailable || err == nil {
		t.Fatalf("status=%d err=%v, want 503 when not logged in", status, err)
	}
}

// --- HTTP endpoint validation ---
// The real auth wrapper is covered by auth_test.go; an identity wrapper is
// used here. These cases all return before any WhatsApp network call.

func TestDeleteEndpointValidation(t *testing.T) {
	identity := func(h http.HandlerFunc) http.HandlerFunc { return h }

	cases := []struct {
		name       string
		method     string
		body       string
		store      bool // seed a DM inbound message under id "inb1"
		wantStatus int
		jsonBody   bool // error responses after validation are JSON
	}{
		{"wrong method GET", http.MethodGet, "", false, http.StatusMethodNotAllowed, false},
		{"wrong method PUT", http.MethodPut, `{"chat_jid":"x","message_id":"y"}`, false, http.StatusMethodNotAllowed, false},
		{"invalid json", http.MethodPost, "{not json", false, http.StatusBadRequest, false},
		{"missing chat_jid", http.MethodPost, `{"message_id":"m"}`, false, http.StatusBadRequest, false},
		{"missing message_id", http.MethodPost, `{"chat_jid":"` + delDMChat + `"}`, false, http.StatusBadRequest, false},
		{"unknown message fails closed", http.MethodDelete, `{"chat_jid":"` + delDMChat + `","message_id":"ghost"}`, false, http.StatusNotFound, true},
		{"dm others message forbidden", http.MethodDelete, `{"chat_jid":"` + delDMChat + `","message_id":"inb1"}`, true, http.StatusForbidden, true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ms := newTestMessageStore(t)
			if tc.store {
				seedDeleteMessage(t, ms, "inb1", delDMChat, delOtherPN, false, time.Now())
			}
			mux := http.NewServeMux()
			registerDeleteEndpoint(mux, identity, newTestClientWithSelf(&mockLIDStore{}, phonePN), ms)

			var body io.Reader
			if tc.body != "" {
				body = strings.NewReader(tc.body)
			}
			req := httptest.NewRequest(tc.method, "/api/messages", body)
			rec := httptest.NewRecorder()
			mux.ServeHTTP(rec, req)

			if rec.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d (body: %s)", rec.Code, tc.wantStatus, rec.Body.String())
			}
			if tc.wantStatus >= 400 && tc.jsonBody {
				var resp DeleteMessageResponse
				if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
					t.Fatalf("body is not JSON DeleteMessageResponse: %v (%s)", err, rec.Body.String())
				}
				if resp.Success {
					t.Fatal("success must be false on error responses")
				}
			}
		})
	}
}

// TestDeleteEndpointLocalMarkOnSend verifies the local deleted_at stamping
// path by driving the handler with a store whose rows are pre-marked — i.e.
// the echo dedup contract: MarkMessageDeleted keeps the first timestamp and
// never resurrects content.
func TestRevokeEchoDedupAfterLocalStamp(t *testing.T) {
	ms := newTestMessageStore(t)
	first := time.Unix(1710000010, 0)
	seedDeleteMessage(t, ms, "own4", delDMChat, phonePN.User, true, time.Unix(1710000000, 0))

	if err := ms.MarkMessageDeleted("own4", delDMChat, first); err != nil {
		t.Fatalf("first mark: %v", err)
	}
	if err := ms.MarkMessageDeleted("own4", delDMChat, time.Unix(1710000099, 0)); err != nil {
		t.Fatalf("second mark: %v", err)
	}

	var got sql.NullTime
	if err := ms.db.QueryRow(
		"SELECT deleted_at FROM messages WHERE id = ? AND chat_jid = ?", "own4", delDMChat,
	).Scan(&got); err != nil {
		t.Fatalf("read deleted_at: %v", err)
	}
	if !got.Valid || !got.Time.Equal(first) {
		t.Fatalf("deleted_at = %v, want earliest %v", got, first)
	}
}
