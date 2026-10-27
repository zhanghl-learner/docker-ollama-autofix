"use strict";

const { shippingCents } = require("./pricing");

/** Return the complete checkout charge in cents. */
function checkoutTotal(subtotalCents) {
  return subtotalCents + shippingCents(subtotalCents);
}

module.exports = { checkoutTotal };
