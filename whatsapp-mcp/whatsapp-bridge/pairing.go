package main

import (
	"bytes"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"net/http"
	"sync"
	"time"

	rscqr "rsc.io/qr"
)

// Live QR availability for browser-based pairing.
//
// whatsmeow rotates the pairing QR every ~20 seconds and only the first
// rotation used to be printed to the terminal, so anyone scanning from a
// screenshot or stale log almost always hit "could not link device". These
// endpoints expose the CURRENT code so a web page can always render a fresh
// one. All routes are bearer-token protected like the rest of /api/*.

var (
	qrMu         sync.RWMutex
	latestQRCode string
	latestQRAt   time.Time
)

// storeLatestQR records every QR rotation coming off the whatsmeow channel.
func storeLatestQR(code string) {
	qrMu.Lock()
	defer qrMu.Unlock()
	latestQRCode = code
	latestQRAt = time.Now()
}

func currentQR() (string, time.Time, bool) {
	qrMu.RLock()
	defer qrMu.RUnlock()
	return latestQRCode, latestQRAt, latestQRCode != ""
}

// registerPairingEndpoints wires GET /api/qr/meta and GET /api/qr.png.
func registerPairingEndpoints(mux *http.ServeMux, auth func(http.HandlerFunc) http.HandlerFunc) {
	mux.HandleFunc("/api/qr/meta", auth(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_, at, ok := currentQR()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			_ = json.NewEncoder(w).Encode(map[string]any{"available": false})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"available":    true,
			"generated_at": at.Unix(),
		})
	}))

	mux.HandleFunc("/api/qr.png", auth(func(w http.ResponseWriter, r *http.Request) {
		code, _, ok := currentQR()
		if !ok {
			http.Error(w, "no QR available yet", http.StatusNotFound)
			return
		}
		enc, err := rscqr.Encode(code, rscqr.H)
		if err != nil {
			http.Error(w, "qr encode failed", http.StatusInternalServerError)
			return
		}

		// Render straight from Code.Bitmap/Size/Stride. rsc.io/qr's Image()
		// is inconsistent (Bounds are Scale-multiplied while At() indexes
		// raw modules), so any draw.Image approach samples mostly
		// out-of-range white — producing a tiny corner code.
		const quietModules = 4 // spec-recommended white margin

		size := enc.Size
		scale := (640 + size - 1) / size
		if scale < 2 {
			scale = 2
		} else if scale > 12 {
			scale = 12
		}
		total := (size + 2*quietModules) * scale

		canvas := image.NewRGBA(image.Rect(0, 0, total, total))
		black := color.RGBA{R: 0, G: 0, B: 0, A: 255}
		white := color.RGBA{R: 255, G: 255, B: 255, A: 255}
		for cy := 0; cy < total; cy++ {
			my := cy/scale - quietModules
			rowBlack := my >= 0 && my < size
			for cx := 0; cx < total; cx++ {
				mx := cx/scale - quietModules
				dark := false
				if rowBlack && mx >= 0 && mx < size {
					dark = enc.Bitmap[my*enc.Stride+mx/8]&(1<<uint(7-mx&7)) != 0
				}
				if dark {
					canvas.SetRGBA(cx, cy, black)
				} else {
					canvas.SetRGBA(cx, cy, white)
				}
			}
		}

		var buf bytes.Buffer
		if err := png.Encode(&buf, canvas); err != nil {
			http.Error(w, "png encode failed", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "image/png")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(buf.Bytes())
	}))
}
