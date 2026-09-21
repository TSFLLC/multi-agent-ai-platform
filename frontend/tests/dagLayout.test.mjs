import { test } from "node:test";
import assert from "node:assert/strict";
import { LAYOUT, computeLayout, edgePath, extentOf } from "../assets/js/dagLayout.js";
import { exampleDag } from "../assets/js/workflowGraph.js";

// Build {nodes, edges} with ids "id-<key>" from the example spec (or any spec).
function fromSpec(spec) {
  const nodes = spec.nodes.map((node) => ({ id: `id-${node.key}`, key: node.key }));
  const edges = spec.edges.map(([from, to]) => ({ id: `e-${from}-${to}`, from: `id-${from}`, to: `id-${to}` }));
  return { nodes, edges };
}

function layerKeys(layout, nodes) {
  const keyOf = Object.fromEntries(nodes.map((n) => [n.id, n.key]));
  return layout.layers.map((layer) => layer.map((id) => keyOf[id]));
}

test("an empty graph lays out to nothing without throwing", () => {
  const layout = computeLayout([], []);
  assert.deepEqual(layout.layers, []);
  assert.deepEqual(layout.positions, {});
  assert.equal(layout.width, LAYOUT.padding * 2);
  assert.equal(layout.height, LAYOUT.padding * 2);
});

test("a sequential path becomes one node per layer, top to bottom", () => {
  const { nodes, edges } = fromSpec({ nodes: [{ key: "a" }, { key: "b" }, { key: "c" }], edges: [["a", "b"], ["b", "c"]] });
  const layout = computeLayout(nodes, edges);
  assert.deepEqual(layerKeys(layout, nodes), [["a"], ["b"], ["c"]]);
  const ys = ["a", "b", "c"].map((k) => layout.positions[`id-${k}`].y);
  assert.ok(ys[0] < ys[1] && ys[1] < ys[2]);
});

test("fan-out puts parallel branches side by side in ONE layer, ordered by node_key", () => {
  const { nodes, edges } = fromSpec({
    nodes: [{ key: "start" }, { key: "zeta" }, { key: "alpha" }, { key: "mid" }],
    edges: [["start", "zeta"], ["start", "alpha"], ["start", "mid"]],
  });
  const layout = computeLayout(nodes, edges);
  assert.deepEqual(layerKeys(layout, nodes), [["start"], ["alpha", "mid", "zeta"]]);
  const xs = ["alpha", "mid", "zeta"].map((k) => layout.positions[`id-${k}`].x);
  assert.ok(xs[0] < xs[1] && xs[1] < xs[2]);
  const ys = new Set(["alpha", "mid", "zeta"].map((k) => layout.positions[`id-${k}`].y));
  assert.equal(ys.size, 1);
});

test("fan-in places the join BELOW its deepest parent (longest path)", () => {
  const { nodes, edges } = fromSpec({
    nodes: [{ key: "a" }, { key: "b" }, { key: "c" }, { key: "join" }],
    edges: [["a", "b"], ["b", "join"], ["a", "c"], ["c", "join"], ["a", "join"]],
  });
  const layout = computeLayout(nodes, edges);
  assert.deepEqual(layerKeys(layout, nodes), [["a"], ["b", "c"], ["join"]]);
});

test("the example DAG has the sequential and parallel structure the diagram shows", () => {
  const { nodes, edges } = fromSpec(exampleDag());
  const layout = computeLayout(nodes, edges);
  assert.deepEqual(layerKeys(layout, nodes), [
    ["planner"],
    ["engineer"],
    ["code", "security", "test"],
    ["eval_code", "eval_security", "eval_test"],
    ["approval"],
    ["complete"],
  ]);
  assert.deepEqual(layout.backEdges, []);
});

test("the layout is deterministic: input order of nodes and edges never changes the result", () => {
  const { nodes, edges } = fromSpec(exampleDag());
  const baseline = computeLayout(nodes, edges);
  for (let seed = 1; seed <= 6; seed += 1) {
    const shuffledNodes = [...nodes].sort((a, b) => ((a.key.charCodeAt(0) * seed) % 7) - ((b.key.charCodeAt(0) * seed) % 7));
    const shuffledEdges = [...edges].reverse().sort((a, b) => ((a.id.length * seed) % 5) - ((b.id.length * seed) % 5));
    const again = computeLayout(shuffledNodes, shuffledEdges);
    assert.deepEqual(again.positions, baseline.positions);
    assert.deepEqual(again.layers, baseline.layers);
  }
});

test("node_key is the stable ordering input, with the id only as a last tie-break", () => {
  const nodes = [
    { id: "2", key: "same" },
    { id: "1", key: "same" },
    { id: "3", key: "aaa" },
  ];
  const layout = computeLayout(nodes, []);
  assert.deepEqual(layout.layers, [["3", "1", "2"]]);
});

test("a wide layer is centred over the widest layer", () => {
  const { nodes, edges } = fromSpec({
    nodes: [{ key: "root" }, { key: "a" }, { key: "b" }, { key: "c" }],
    edges: [["root", "a"], ["root", "b"], ["root", "c"]],
  });
  const layout = computeLayout(nodes, edges);
  const root = layout.positions["id-root"];
  const b = layout.positions["id-b"];
  assert.equal(root.x, b.x); // the single root sits above the middle of three
});

test("incomplete drafts are safe: unknown endpoints, self-edges and duplicate edges are ignored", () => {
  const nodes = [{ id: "a", key: "a" }, { id: "b", key: "b" }, { id: "lonely", key: "lonely" }];
  const edges = [
    { from: "a", to: "b" },
    { from: "a", to: "b" },
    { from: "a", to: "a" },
    { from: "a", to: "ghost" },
    { from: "ghost", to: "b" },
    null,
    undefined,
  ];
  const layout = computeLayout(nodes, edges);
  assert.deepEqual(layout.layers, [["a", "lonely"], ["b"]]);
  assert.equal(Object.keys(layout.positions).length, 3);
});

test("a nodeless edge list and null inputs do not throw", () => {
  assert.doesNotThrow(() => computeLayout(null, null));
  assert.doesNotThrow(() => computeLayout([{ id: "x", key: null }], [{ from: "x", to: "x" }]));
});

test("a cycle in an invalid draft is set aside as a back edge and every node is still placed", () => {
  const { nodes, edges } = fromSpec({
    nodes: [{ key: "a" }, { key: "b" }, { key: "c" }],
    edges: [["a", "b"], ["b", "c"], ["c", "a"]],
  });
  const layout = computeLayout(nodes, edges);
  assert.equal(Object.keys(layout.positions).length, 3);
  assert.equal(layout.backEdges.length, 1);
  assert.deepEqual(layout.backEdges[0], { from: "id-c", to: "id-a" });
  assert.deepEqual(layerKeys(layout, nodes), [["a"], ["b"], ["c"]]);
});

test("a fully cyclic pair and a self-referencing draft never loop forever", () => {
  const nodes = [{ id: "a", key: "a" }, { id: "b", key: "b" }];
  const layout = computeLayout(nodes, [{ from: "a", to: "b" }, { from: "b", to: "a" }]);
  assert.equal(Object.keys(layout.positions).length, 2);
  assert.equal(layout.backEdges.length, 1);
});

test("a disconnected island is placed, not dropped", () => {
  const { nodes, edges } = fromSpec({
    nodes: [{ key: "a" }, { key: "b" }, { key: "island" }],
    edges: [["a", "b"]],
  });
  const layout = computeLayout(nodes, edges);
  assert.ok(layout.positions["id-island"]);
});

test("crossing reduction keeps children next to their own parent", () => {
  // a1 -> x1, a2 -> x2 with parents ordered by key; children must follow, not cross.
  const { nodes, edges } = fromSpec({
    nodes: [{ key: "a1" }, { key: "a2" }, { key: "y" }, { key: "z" }],
    edges: [["a1", "z"], ["a2", "y"]],
  });
  const layout = computeLayout(nodes, edges);
  // z hangs under a1 (left), y under a2 (right): key order alone (y, z) would cross.
  assert.deepEqual(layerKeys(layout, nodes), [["a1", "a2"], ["z", "y"]]);
});

test("extentOf sizes the drawing area from dragged positions", () => {
  const extent = extentOf({ a: { x: 500, y: 300 } });
  assert.equal(extent.width, 500 + LAYOUT.nodeWidth + LAYOUT.padding);
  assert.equal(extent.height, 300 + LAYOUT.nodeHeight + LAYOUT.padding);
  assert.deepEqual(extentOf({}), { width: LAYOUT.padding, height: LAYOUT.padding });
});

test("edgePath goes from the source's bottom-centre to the target's top-centre", () => {
  const from = { x: 0, y: 0 };
  const to = { x: 240, y: 200 };
  const path = edgePath(from, to);
  assert.deepEqual(path.start, { x: LAYOUT.nodeWidth / 2, y: LAYOUT.nodeHeight });
  assert.deepEqual(path.end, { x: 240 + LAYOUT.nodeWidth / 2, y: 200 });
  assert.match(path.d, /^M [\d.]+ [\d.]+ C /);
});

test("an upward edge (a cycle) still gets a finite path", () => {
  const path = edgePath({ x: 0, y: 400 }, { x: 0, y: 0 });
  assert.match(path.d, /^M /);
  assert.ok(!path.d.includes("NaN"));
});
