# SEO Bot

Local site auditor with a browser UI.

## Run

```bash
cd /home/user/Work/seo-bot
python3 server.py
```

Open http://127.0.0.1:8765. Paste a public URL, choose how many pages to crawl (1–30), and start the audit.

The report scores each HTML page on title, meta description, H1, language, viewport, canonical, word count, image alt text, Open Graph tags, and internal links. It also checks `robots.txt` and a sitemap.

## Rewrites

Title and description drafts call the xAI API with model `grok-4.7`. Export a key before starting the server:

```bash
export XAI_API_KEY=your-key
python3 server.py
```

The key stays on the server. The page itself never sees it.

## Limits

The server listens only on 127.0.0.1. It refuses private-network hosts. It does not change the live website; it reports what to fix.
