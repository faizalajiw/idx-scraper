# MCP Server Configuration for Hermes

This MCP server exposes IDX (Indonesia Stock Exchange) data scraping tools.

## How to Connect

1. Open Hermes CLI:
```bash
hermes mcp add idx-scraper "node C:\Project\idx-scraper\mcp\server.js"
```

2. Or manually edit `~\AppData\Local\hermes\mcp-config.yaml`:
```yaml
servers:
  idx-scraper:
    command: node
    args:
      - C:\Project\idx-scraper\mcp\server.js
    type: stdio
```

3. Restart Hermes:
```bash
hermes
```

## Available Tools

- `get_idx_index` - Get current index data (IHSG, LQ45, etc.)
- `get_stock_price` - Get current stock price for a ticker
- `get_all_indices` - Get all available index data
- `get_index_history` - Get historical index data
- `get_stock_history` - Get historical stock data
- `get_watchlist_prices` - Get prices for all tracked stocks

## Database

Default: `d:\Project\idx-scraper\data\idx.db`

To change, set `IDX_SQLITE_PATH` environment variable before running the server.
