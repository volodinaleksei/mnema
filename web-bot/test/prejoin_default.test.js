// Testy znacheniya BOT_PREJOIN_SECONDS (config.js). Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadConfig } from "../src/config.js";

test("BOT_PREJOIN_SECONDS ne zadan — 300 s (5 min) po umolchaniyu", () => {
  const saved = process.env.BOT_PREJOIN_SECONDS;
  delete process.env.BOT_PREJOIN_SECONDS;
  try {
    assert.equal(loadConfig().prejoinSeconds, 300);
  } finally {
    if (saved !== undefined) process.env.BOT_PREJOIN_SECONDS = saved;
  }
});

test("BOT_PREJOIN_SECONDS iz env perebivaet default", () => {
  const saved = process.env.BOT_PREJOIN_SECONDS;
  process.env.BOT_PREJOIN_SECONDS = "600";
  try {
    assert.equal(loadConfig().prejoinSeconds, 600);
  } finally {
    if (saved !== undefined) process.env.BOT_PREJOIN_SECONDS = saved;
    else delete process.env.BOT_PREJOIN_SECONDS;
  }
});

test("BOT_PREJOIN_SECONDS neraspoznnannoe — default 300", () => {
  const saved = process.env.BOT_PREJOIN_SECONDS;
  process.env.BOT_PREJOIN_SECONDS = "ne-chislo";
  try {
    assert.equal(loadConfig().prejoinSeconds, 300);
  } finally {
    if (saved !== undefined) process.env.BOT_PREJOIN_SECONDS = saved;
    else delete process.env.BOT_PREJOIN_SECONDS;
  }
});
