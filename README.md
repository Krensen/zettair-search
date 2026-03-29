# Zettair Search Service

A lightweight FastAPI web service wrapping the Zettair BM25 search engine.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
python server.py
```

Then open http://localhost:8765 in your browser.

## Config (env vars)

| Variable     | Default                              | Description               |
|-------------|--------------------------------------|---------------------------|
| `ZET_BINARY` | `../zettair/devel/zet`               | Path to zet executable    |
| `ZET_INDEX`  | `../zettair/testindex/index`         | Path to index (no suffix) |
| `ZET_PORT`   | `8765`                               | Port to listen on         |

Example with custom index:
```bash
ZET_INDEX=/path/to/myindex ZET_PORT=9000 python server.py
```

## API

### `GET /search`

| Param | Type | Description |
|-------|------|-------------|
| `q`   | string | Search query (required) |
| `n`   | int | Number of results, 1–100 (default: 10) |

**Response:**
```json
{
  "query": "white whale",
  "total": 852,
  "took_ms": 0.617,
  "results": [
    {
      "rank": 1,
      "score": 5.96,
      "docid": 773,
      "title": "Chapter 36, Paragraph 25",
      "snippet": "It's a white whale, I say..."
    }
  ]
}
```
