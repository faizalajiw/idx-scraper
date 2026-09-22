#!/usr/bin/env node

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import sqlite3 from "better-sqlite3";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Database path - read from env or default
const DB_PATH = process.env.IDX_SQLITE_PATH || path.join(__dirname, "..", "data", "idx.db");

function getDb() {
  const db = sqlite3(DB_PATH);
  db.pragma("journal_mode = WAL");
  return db;
}

const server = new Server(
  {
    name: "idx-scraper-mcp",
    version: "1.0.0",
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "get_idx_index",
      description: "Get current index data (IHSG, LQ45, etc.) from IDX",
      inputSchema: {
        type: "object",
        properties: {
          code: {
            type: "string",
            description: "Index code (COMPOSITE for IHSG, LQ45, etc.)",
            enum: ["COMPOSITE", "LQ45", "IDX30", "IDX80"],
          },
        },
        required: ["code"],
      },
    },
    {
      name: "get_stock_price",
      description: "Get current stock price for a specific ticker (BBCA, BBRI, etc.)",
      inputSchema: {
        type: "object",
        properties: {
          code: {
            type: "string",
            description: "Stock ticker code (e.g., BBCA, BBRI, TLKM)",
          },
        },
        required: ["code"],
      },
    },
    {
      name: "get_all_indices",
      description: "Get all available index data",
      inputSchema: {
        type: "object",
        properties: {},
      },
    },
    {
      name: "get_index_history",
      description: "Get historical index data",
      inputSchema: {
        type: "object",
        properties: {
          code: {
            type: "string",
            description: "Index code",
          },
          limit: {
            type: "integer",
            description: "Number of records to return",
            default: 10,
          },
        },
        required: ["code"],
      },
    },
    {
      name: "get_stock_history",
      description: "Get historical stock data",
      inputSchema: {
        type: "object",
        properties: {
          code: {
            type: "string",
            description: "Stock ticker code",
          },
          limit: {
            type: "integer",
            description: "Number of records to return",
            default: 10,
          },
        },
        required: ["code"],
      },
    },
    {
      name: "get_watchlist_prices",
      description: "Get prices for all tracked watchlist stocks",
      inputSchema: {
        type: "object",
        properties: {},
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request;
  const db = getDb();

  try {
    switch (name) {
      case "get_idx_index": {
        const row = db
          .prepare(
            `SELECT source, code, close, change, percent, current, captured_at 
             FROM index_quotes 
             WHERE code = ? 
             ORDER BY captured_at DESC 
             LIMIT 1`
          )
          .get(args.code.toUpperCase());

        if (!row) {
          return {
            content: [{ type: "text", text: `Index ${args.code} not found` }],
            isError: true,
          };
        }

        return {
          content: [
            {
              type: "text",
              text: `Index ${row.code}:\n- Close: ${row.close}\n- Change: ${row.change}\n- Current: ${row.current}\n- Captured at: ${row.captured_at}`,
            },
          ],
        };
      }

      case "get_stock_price": {
        const row = db
          .prepare(
            `SELECT source, code, close, volume, bid, offer, captured_at 
             FROM stock_quotes 
             WHERE code = ? 
             ORDER BY captured_at DESC 
             LIMIT 1`
          )
          .get(args.code.toUpperCase());

        if (!row) {
          return {
            content: [{ type: "text", text: `Stock ${args.code} not found` }],
            isError: true,
          };
        }

        return {
          content: [
            {
              type: "text",
              text: `Stock ${row.code}:\n- Price: ${row.close}\n- Volume: ${row.volume}\n- Bid: ${row.bid}\n- Offer: ${row.offer}\n- Captured at: ${row.captured_at}`,
            },
          ],
        };
      }

      case "get_all_indices": {
        const rows = db
          .prepare(
            `SELECT DISTINCT code FROM index_quotes 
             ORDER BY code`
          )
          .all();

        const indices = {};
        for (const { code } of rows) {
          const row = db
            .prepare(
              `SELECT code, close, change, percent, current, captured_at 
               FROM index_quotes 
               WHERE code = ? 
               ORDER BY captured_at DESC 
               LIMIT 1`
            )
            .get(code);
          indices[code] = row;
        }

        return {
          content: [{ type: "text", text: JSON.stringify(indices, null, 2) }],
        };
      }

      case "get_index_history": {
        const rows = db
          .prepare(
            `SELECT code, close, change, percent, current, captured_at 
             FROM index_quotes 
             WHERE code = ? 
             ORDER BY captured_at DESC 
             LIMIT ?`
          )
          .all(args.code.toUpperCase(), args.limit || 10);

        return {
          content: [{ type: "text", text: JSON.stringify(rows, null, 2) }],
        };
      }

      case "get_stock_history": {
        const rows = db
          .prepare(
            `SELECT code, close, volume, bid, offer, captured_at 
             FROM stock_quotes 
             WHERE code = ? 
             ORDER BY captured_at DESC 
             LIMIT ?`
          )
          .all(args.code.toUpperCase(), args.limit || 10);

        return {
          content: [{ type: "text", text: JSON.stringify(rows, null, 2) }],
        };
      }

      case "get_watchlist_prices": {
        const rows = db
          .prepare(
            `SELECT DISTINCT code FROM stock_quotes 
             ORDER BY code`
          )
          .all();

        const prices = {};
        for (const { code } of rows) {
          const row = db
            .prepare(
              `SELECT code, close, volume, bid, offer, captured_at 
               FROM stock_quotes 
               WHERE code = ? 
               ORDER BY captured_at DESC 
               LIMIT 1`
            )
            .get(code);
          prices[code] = row;
        }

        return {
          content: [{ type: "text", text: JSON.stringify(prices, null, 2) }],
        };
      }

      default:
        return {
          content: [{ type: "text", text: `Unknown tool: ${name}` }],
          isError: true,
        };
    }
  } catch (error) {
    return {
      content: [{ type: "text", text: `Error: ${error.message}` }],
      isError: true,
    };
  } finally {
    db.close();
  }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("IDX Scraper MCP server running on stdio");
}

main().catch((error) => {
  console.error("Fatal error:", error);
  process.exit(1);
});
