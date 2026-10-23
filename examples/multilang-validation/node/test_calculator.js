const { add } = require("./calculator");

const actual = add(2, 3);
if (actual !== 5) {
  console.error(`Expected add(2, 3) to equal 5, received ${actual}`);
  process.exit(1);
}

console.log("Node test passed");
