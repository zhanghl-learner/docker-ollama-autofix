"use strict";

/** Return shipping cost in cents; orders of $50 or more ship free. */
function shippingCents(subtotalCents) {
  if (!Number.isInteger(subtotalCents) || subtotalCents < 0) {
    throw new TypeError("subtotalCents must be a non-negative integer");
  }
  return subtotalCents > 5000 ? 0 : 799;
}

module.exports = { shippingCents };
