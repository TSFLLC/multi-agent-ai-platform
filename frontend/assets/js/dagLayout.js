// Deterministic layered layout for the Workflow Studio canvas (MA7.6A) -- pure,
// DOM-free, unit-tested directly (frontend/tests/dagLayout.test.mjs).
//
// Layers come from the graph's own topology (longest path from an entry node), so
// sequential steps stack top to bottom and parallel branches sit side by side in
// one layer. The order inside a layer starts from node_key (the stable input) and
// is then refined by a few barycenter sweeps to cut crossings -- with ties always
// broken by node_key, so the same graph gives the same picture whatever order the
// API returned nodes/edges in.
//
// It must never throw or loop on a draft that is invalid or half built: unknown
// edge endpoints, self-edges and duplicates are ignored, and edges that close a
// cycle are set aside (reported in `backEdges`) so the remaining graph can still
// be layered. The server's validator is what rejects such a draft; the canvas
// just has to draw something sensible while the user fixes it.

export const LAYOUT = {
  nodeWidth: 200,
  nodeHeight: 92,
  gapX: 40,
  gapY: 76,
  padding: 40,
};

function compareNodes(a, b) {
  const ak = String(a.key == null ? "" : a.key);
  const bk = String(b.key == null ? "" : b.key);
  if (ak < bk) return -1;
  if (ak > bk) return 1;
  const ai = String(a.id);
  const bi = String(b.id);
  return ai < bi ? -1 : ai > bi ? 1 : 0;
}

export function computeLayout(nodes, edges, options = {}) {
  const cfg = { ...LAYOUT, ...options };
  const sorted = (nodes || []).filter((node) => node && node.id != null).sort(compareNodes);
  const order = new Map(sorted.map((node, index) => [node.id, index]));

  // -- sanitize edges: known endpoints, no self-edges, no duplicates
  const seen = new Set();
  const valid = [];
  for (const edge of edges || []) {
    if (!edge || edge.from === edge.to || !order.has(edge.from) || !order.has(edge.to)) continue;
    const key = `${edge.from}\u0000${edge.to}`;
    if (seen.has(key)) continue;
    seen.add(key);
    valid.push({ from: edge.from, to: edge.to });
  }

  const out = new Map(sorted.map((node) => [node.id, []]));
  for (const edge of valid) out.get(edge.from).push(edge.to);
  for (const targets of out.values()) targets.sort((a, b) => order.get(a) - order.get(b));

  // -- set aside back edges (iterative DFS in key order) so what remains is a DAG
  const color = new Map(sorted.map((node) => [node.id, 0])); // 0 unseen, 1 on stack, 2 done
  const backEdgeKeys = new Set();
  for (const root of sorted) {
    if (color.get(root.id) !== 0) continue;
    const stack = [[root.id, 0]];
    color.set(root.id, 1);
    while (stack.length) {
      const frame = stack[stack.length - 1];
      const targets = out.get(frame[0]);
      if (frame[1] < targets.length) {
        const next = targets[frame[1]];
        frame[1] += 1;
        if (color.get(next) === 0) {
          color.set(next, 1);
          stack.push([next, 0]);
        } else if (color.get(next) === 1) {
          backEdgeKeys.add(`${frame[0]}\u0000${next}`);
        }
      } else {
        color.set(frame[0], 2);
        stack.pop();
      }
    }
  }
  const forward = valid.filter((edge) => !backEdgeKeys.has(`${edge.from}\u0000${edge.to}`));
  const backEdges = valid.filter((edge) => backEdgeKeys.has(`${edge.from}\u0000${edge.to}`));

  // -- longest-path layering (Kahn, key order)
  const preds = new Map(sorted.map((node) => [node.id, []]));
  const succs = new Map(sorted.map((node) => [node.id, []]));
  const indegree = new Map(sorted.map((node) => [node.id, 0]));
  for (const edge of forward) {
    preds.get(edge.to).push(edge.from);
    succs.get(edge.from).push(edge.to);
    indegree.set(edge.to, indegree.get(edge.to) + 1);
  }
  const rank = new Map(sorted.map((node) => [node.id, 0]));
  const ready = sorted.filter((node) => indegree.get(node.id) === 0).map((node) => node.id);
  while (ready.length) {
    ready.sort((a, b) => order.get(a) - order.get(b));
    const id = ready.shift();
    for (const next of succs.get(id)) {
      rank.set(next, Math.max(rank.get(next), rank.get(id) + 1));
      indegree.set(next, indegree.get(next) - 1);
      if (indegree.get(next) === 0) ready.push(next);
    }
  }

  const layerCount = sorted.length ? Math.max(...rank.values()) + 1 : 0;
  const layers = Array.from({ length: layerCount }, () => []);
  for (const node of sorted) layers[rank.get(node.id)].push(node.id); // already in key order

  // -- barycenter sweeps; ties always fall back to key order
  const fraction = new Map();
  const refresh = () => {
    for (const layer of layers) layer.forEach((id, index) => fraction.set(id, (index + 0.5) / layer.length));
  };
  const sweep = (layer, neighbours) => {
    const scored = layer.map((id) => {
      const list = neighbours.get(id);
      const score = list.length ? list.reduce((sum, other) => sum + fraction.get(other), 0) / list.length : fraction.get(id);
      return { id, score };
    });
    scored.sort((a, b) => a.score - b.score || order.get(a.id) - order.get(b.id));
    return scored.map((entry) => entry.id);
  };
  refresh();
  for (let pass = 0; pass < 4; pass += 1) {
    for (let l = 1; l < layers.length; l += 1) {
      layers[l] = sweep(layers[l], preds);
      refresh();
    }
    for (let l = layers.length - 2; l >= 0; l -= 1) {
      layers[l] = sweep(layers[l], succs);
      refresh();
    }
  }

  // -- coordinates
  const widest = layers.reduce((max, layer) => Math.max(max, layer.length), 0);
  const contentWidth = widest ? widest * cfg.nodeWidth + (widest - 1) * cfg.gapX : 0;
  const positions = {};
  layers.forEach((layer, layerIndex) => {
    const layerWidth = layer.length * cfg.nodeWidth + (layer.length - 1) * cfg.gapX;
    const startX = cfg.padding + (contentWidth - layerWidth) / 2;
    layer.forEach((id, index) => {
      positions[id] = {
        x: startX + index * (cfg.nodeWidth + cfg.gapX),
        y: cfg.padding + layerIndex * (cfg.nodeHeight + cfg.gapY),
        layer: layerIndex,
        order: index,
      };
    });
  });

  const height = layerCount ? layerCount * cfg.nodeHeight + (layerCount - 1) * cfg.gapY : 0;
  return {
    positions,
    layers,
    backEdges,
    width: contentWidth + cfg.padding * 2,
    height: height + cfg.padding * 2,
    config: cfg,
  };
}

// The size of the drawing area for a set of (possibly dragged) positions.
export function extentOf(positions, cfg = LAYOUT) {
  let width = 0;
  let height = 0;
  for (const position of Object.values(positions || {})) {
    width = Math.max(width, position.x + cfg.nodeWidth);
    height = Math.max(height, position.y + cfg.nodeHeight);
  }
  return { width: width + cfg.padding, height: height + cfg.padding };
}

// SVG path for a directed edge: from the source card's bottom-centre to the
// target card's top-centre. Edges that point up or sideways (a cycle in an
// invalid draft) bow out to the side so they stay visible rather than folding
// onto the cards.
export function edgePath(fromPosition, toPosition, cfg = LAYOUT) {
  const x1 = fromPosition.x + cfg.nodeWidth / 2;
  const y1 = fromPosition.y + cfg.nodeHeight;
  const x2 = toPosition.x + cfg.nodeWidth / 2;
  const y2 = toPosition.y;
  const dy = y2 - y1;
  let d;
  if (dy >= 24) {
    const bend = Math.max(28, dy / 2);
    d = `M ${x1} ${y1} C ${x1} ${y1 + bend}, ${x2} ${y2 - bend}, ${x2} ${y2}`;
  } else {
    const side = Math.max(cfg.nodeWidth / 2 + 40, Math.abs(x2 - x1) / 2 + 60);
    const direction = x2 >= x1 ? 1 : -1;
    d = `M ${x1} ${y1} C ${x1 + direction * side} ${y1 + 60}, ${x2 + direction * side} ${y2 - 60}, ${x2} ${y2}`;
  }
  return { d, start: { x: x1, y: y1 }, end: { x: x2, y: y2 }, mid: { x: (x1 + x2) / 2, y: (y1 + y2) / 2 } };
}
