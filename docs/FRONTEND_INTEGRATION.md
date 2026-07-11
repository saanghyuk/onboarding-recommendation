# Frontend Integration

**Purpose**: teach an iOS / Android / Web engineer exactly which endpoints to call, with what payloads, and when.

Assume `RECO_BASE = https://reco.example.com` (or `http://localhost:8000` in dev).

---

## 0. Contract in one paragraph

Persist a `session_id` UUID client-side. On quiz answers call `POST /api/quiz-log`. When the user finishes the quiz, call `GET /api/recommendations?style=…&price=…&item=…&session_id=…&k=60`. For every subsequent interaction on a returned product, fire `POST /api/feedback` with the `matched_key` from the response. That's it.

---

## 1. Data flow

```
Client                                            Server

App first launch
  session_id = uuid()  (persisted)

Quiz step
  POST /api/quiz-log {step, answers, session_id}

Quiz complete
  GET /api/recommendations?style=…&price=…&item=…&k=60&session_id=…
  → 200 { products: [...], matched_key: "golf__mid__browse", is_fallback: false }

For each rendered card
  If tapped:                  POST /api/feedback { signal: "click" }
  If viewport dwell ≥ 2 s:    POST /api/feedback { signal: "dwell_2s" }
  If scrolled past < 300 ms:  POST /api/feedback { signal: "skip" }

On checkout success
  POST /api/feedback { signal: "purchase" }
```

Impressions are recorded server-side. Do **not** fire an explicit impression event.

---

## 2. Session ID management

### 2.1 iOS (Swift)
```swift
import Foundation

enum RecoSession {
    static let key = "reco_session_id"

    static func getOrCreate() -> String {
        if let existing = UserDefaults.standard.string(forKey: key) {
            return existing
        }
        let newId = UUID().uuidString
        UserDefaults.standard.set(newId, forKey: key)
        return newId
    }
}
```

### 2.2 Android (Kotlin)
```kotlin
object RecoSession {
    private const val PREF_NAME = "reco_pref"
    private const val KEY = "reco_session_id"

    fun getOrCreate(context: Context): String {
        val prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
        prefs.getString(KEY, null)?.let { return it }
        val newId = UUID.randomUUID().toString()
        prefs.edit().putString(KEY, newId).apply()
        return newId
    }
}
```

### 2.3 Web (TypeScript)
```typescript
export function getOrCreateSessionId(): string {
  let sid = localStorage.getItem('reco_session_id');
  if (!sid) {
    sid = crypto.randomUUID();
    localStorage.setItem('reco_session_id', sid);
  }
  return sid;
}
```

---

## 3. Client examples

### 3.1 iOS (URLSession)
```swift
struct RecoClient {
    static let baseURL = "https://reco.example.com"
    static var sessionId: String { RecoSession.getOrCreate() }

    static func logQuizStep(step: String, answers: [String: String]) async throws {
        try await post("/api/quiz-log", body: [
            "step": step, "answers": answers,
            "session_id": sessionId,
            "device": ["os": "iOS"]
        ])
    }

    static func getRecommendations(style: String, price: String, item: String, k: Int = 60) async throws -> RecoResponse {
        let url = URL(string:
            "\(baseURL)/api/recommendations?style=\(style)&price=\(price)&item=\(item)&k=\(k)&session_id=\(sessionId)&os_name=iOS"
        )!
        let (data, _) = try await URLSession.shared.data(from: url)
        return try JSONDecoder().decode(RecoResponse.self, from: data)
    }

    static func sendFeedback(cohortKey: String, productId: String, signal: FeedbackSignal) async throws {
        try await post("/api/feedback", body: [
            "cohort_key": cohortKey,
            "product_id": productId,
            "signal": signal.rawValue,
            "session_id": sessionId
        ])
    }

    private static func post(_ path: String, body: [String: Any]) async throws {
        var req = URLRequest(url: URL(string: "\(baseURL)\(path)")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        _ = try await URLSession.shared.data(for: req)
    }
}

enum FeedbackSignal: String { case click, purchase, skip, dwell_2s }

struct RecoResponse: Decodable {
    let matched_key: String
    let is_fallback: Bool
    let products: [Product]
    // `top12` alias also decodes if needed
}
```

Decode both fields safely:
```swift
enum CodingKeys: String, CodingKey { case matched_key, is_fallback, products, top12 }
init(from decoder: Decoder) throws {
    let c = try decoder.container(keyedBy: CodingKeys.self)
    matched_key = try c.decode(String.self, forKey: .matched_key)
    is_fallback = try c.decode(Bool.self, forKey: .is_fallback)
    // prefer `products`, fall back to `top12` for older builds
    products = (try? c.decode([Product].self, forKey: .products))
             ?? (try c.decode([Product].self, forKey: .top12))
}
```

### 3.2 Android (Kotlin + OkHttp)
```kotlin
object RecoClient {
    private const val BASE = "https://reco.example.com"
    private val JSON = "application/json".toMediaType()
    private val client = OkHttpClient()

    fun sessionId(ctx: Context) = RecoSession.getOrCreate(ctx)

    fun logQuizStep(ctx: Context, step: String, answers: Map<String, String>) {
        val body = JSONObject().apply {
            put("step", step)
            put("answers", JSONObject(answers))
            put("device", JSONObject(mapOf("os" to "Android")))
            put("session_id", sessionId(ctx))
        }
        post(ctx, "/api/quiz-log", body)
    }

    fun sendFeedback(ctx: Context, cohortKey: String, productId: String, signal: String) {
        val body = JSONObject().apply {
            put("cohort_key", cohortKey)
            put("product_id", productId)
            put("signal", signal)
            put("session_id", sessionId(ctx))
        }
        post(ctx, "/api/feedback", body)
    }

    private fun post(ctx: Context, path: String, body: JSONObject) {
        val req = Request.Builder()
            .url("$BASE$path")
            .post(body.toString().toRequestBody(JSON))
            .build()
        client.newCall(req).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) { /* log */ }
            override fun onResponse(call: Call, response: Response) { response.close() }
        })
    }
}
```

### 3.3 Web (TypeScript / fetch)
```typescript
const RECO_BASE = 'https://reco.example.com';
const sessionId = getOrCreateSessionId();

export async function getRecommendations(style: string, price: string, item: string, k = 60) {
  const url = `${RECO_BASE}/api/recommendations?style=${style}&price=${price}&item=${item}&k=${k}&session_id=${sessionId}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`recos failed ${r.status}`);
  const data = await r.json();
  // Prefer `products`; `top12` is a backward-compat alias
  return { ...data, products: data.products ?? data.top12 };
}

export function sendFeedback(cohortKey: string, productId: string, signal: 'click' | 'purchase' | 'skip' | 'dwell_2s') {
  return fetch(`${RECO_BASE}/api/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cohort_key: cohortKey, product_id: productId, signal, session_id: sessionId }),
    keepalive: true,
  });
}

export function logQuizStep(step: string, answers: Record<string, string>) {
  return fetch(`${RECO_BASE}/api/quiz-log`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ step, answers, device: { os: 'Web' }, session_id: sessionId }),
    keepalive: true,
  });
}
```

---

## 4. When to fire each signal

| Signal | Trigger |
|---|---|
| `click` | User taps a product card and navigates to detail |
| `purchase` | Order-completed callback (**not** on checkout attempt) |
| `skip` | Card entered the viewport for less than 300 ms and left |
| `dwell_2s` | Card stayed in the viewport for ≥ 2 s (fire at most once per card per session) |

Use `IntersectionObserver` (web) / `RecyclerView` scroll listeners (Android) / `UICollectionView` visibility (iOS).

---

## 5. Error handling

- `/api/feedback` and `/api/quiz-log` are fire-and-forget — never block UI on their response.
- `/api/recommendations` should retry twice on `5xx`, then fall back to a home / featured screen.
- Consider an offline queue for feedback events (P1 in the [production roadmap](PRODUCTION_ROADMAP.md)).

---

## 6. SwiftUI card example

```swift
struct ProductCard: View {
    let product: Product
    let cohortKey: String
    @State private var dwellTimer: Timer?
    @State private var dwellSent = false

    var body: some View {
        VStack {
            AsyncImage(url: product.imageUrl)
            Text(product.name)
            Text("\(product.price) KRW")
        }
        .onTapGesture {
            Task { try? await RecoClient.sendFeedback(
                cohortKey: cohortKey, productId: product.id, signal: .click) }
        }
        .onAppear {
            dwellSent = false
            dwellTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: false) { _ in
                guard !dwellSent else { return }
                dwellSent = true
                Task { try? await RecoClient.sendFeedback(
                    cohortKey: cohortKey, productId: product.id, signal: .dwell_2s) }
            }
        }
        .onDisappear {
            dwellTimer?.invalidate()
            if !dwellSent {
                Task { try? await RecoClient.sendFeedback(
                    cohortKey: cohortKey, productId: product.id, signal: .skip) }
            }
        }
    }
}
```

---

## 7. NLU (natural language) integration

`/voice` uses `POST /api/nlu` to convert free-form text into structured `(style, price, item)`. The endpoint always returns 200 — when `ANTHROPIC_API_KEY` is not set on the server it falls back to a rule-based parser (`engine: "rule-based"`).

```typescript
const nlu = await fetch(`${RECO_BASE}/api/nlu`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ text: userSpokenText })
}).then(r => r.json());

const { style, price, item, engine } = nlu;
const recos = await getRecommendations(style, price, item);
```

---

## 8. Sanity checks

```bash
# recommendations
curl "$RECO_BASE/api/recommendations?style=golf&price=mid&item=browse&session_id=test-1&k=60"

# feedback
curl -X POST "$RECO_BASE/api/feedback" \
  -H "Content-Type: application/json" \
  -d '{"cohort_key":"golf__mid__browse","product_id":"P001","signal":"click","session_id":"test-1"}'

# NLU
curl -X POST "$RECO_BASE/api/nlu" \
  -H "Content-Type: application/json" \
  -d '{"text":"looking for a golf polo around 50000 KRW"}'

# learning progress
curl "$RECO_BASE/api/bandit-stats" | jq
```

---

## 9. FAQ

**How do I know which `cohort_key` to use in feedback?** Take `matched_key` verbatim from the recommendations response.

**What if the client loses connectivity?** Fire-and-forget for now. Add a local offline queue when it starts to matter (see roadmap).

**When does `session_id` reset?** Only when the client clears storage or reinstalls the app. Cross-device tie-in via `user_id` is out of scope for the demo.

**Does the response give me exactly 12 items?** No — `products` (and its alias `top12`) return up to `k` items (default 60, min 12, max 200). The `top12` name is historical.

---

## Related Files

- `app.py` — endpoint implementations
- [API_SPEC.md](API_SPEC.md) — exact contract
- [MODEL_SPEC.md](MODEL_SPEC.md) — why each signal weight
- [PRODUCTION_ROADMAP.md](PRODUCTION_ROADMAP.md) — offline queue, retry, identity work
