"use strict";

const assert = require("node:assert/strict");
const { checkoutTotal } = require("../src/checkout");
const { shippingCents } = require("../src/pricing");

assert.equal(checkoutTotal(3200), 3999, "ordinary orders include shipping");
assert.equal(shippingCents(5000), 0, "free shipping starts at the boundary");
assert.equal(checkoutTotal(7200), 7200, "larger orders remain free");
assert.throws(() => shippingCents(-1), TypeError, "invalid totals are rejected");

console.log("Node checkout tests passed");
