// The Workflow Studio's diagram (MA7.6A): custom HTML cards over an SVG edge
// layer -- no graph library. Layout maths lives in dagLayout.js (pure, tested);
// this file only turns a view model into DOM and reports user gestures.
//
// Everything user-controlled (node keys, Agent names, labels, model ids) is set
// through textContent / DOM text nodes, never innerHTML.
//
// View model given to render():
//   {
//     nodes:     [{ id, type, title, tag, lines: [..], unsupported, hint, status? }],
//                status (execution view only): { key, label } -- shown as a pill on the card
//     edges (each): { id, from, to, label, done? }   done = its source step has completed
//     edges:     [{ id, from, to, label }],
//     positions: { [nodeId]: { x, y } },
//     selectedNodeId, selectedEdgeId, connectFromId, flaggedIds: Set, editable
//   }
// Handlers: onSelectNode(id|null), onSelectEdge(id|null), onStartConnect(id),
//           onConnect(fromId, toId), onMove(id, x, y), onDelete(), onEscape()

import { el } from "./dom.js";
import { LAYOUT, edgePath, extentOf } from "./dagLayout.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const DRAG_THRESHOLD = 4;

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  return node;
}

// options.draggable: false for the execution view (a fixed layout, nothing to rearrange).
export function createDagCanvas(handlers = {}, options = {}) {
  const draggable = options.draggable !== false;
  const wrapper = el("div", { class: "studio-canvas", tabindex: "0", role: "group", "aria-label": "Workflow diagram" });
  const inner = el("div", { class: "studio-canvas-inner" });
  const edgeLayer = svgEl("svg", { class: "studio-edges", "aria-hidden": "true" });
  const defs = svgEl("defs");
  for (const [id, cls] of [["studio-arrow", ""], ["studio-arrow-selected", "selected"], ["studio-arrow-flag", "flag"]]) {
    const marker = svgEl("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
    marker.appendChild(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", class: `studio-arrowhead ${cls}`.trim() }));
    defs.appendChild(marker);
  }
  edgeLayer.appendChild(defs);
  const edgeGroup = svgEl("g", { class: "studio-edge-group" });
  edgeLayer.appendChild(edgeGroup);
  inner.appendChild(edgeLayer);
  wrapper.appendChild(inner);

  let model = { nodes: [], edges: [], positions: {}, editable: false, flaggedIds: new Set() };
  let positions = {};
  const nodeElements = new Map();
  const portElements = new Map();
  let suppressClick = false;

  function size() {
    const extent = extentOf(positions, LAYOUT);
    inner.style.width = `${Math.max(extent.width, 480)}px`;
    inner.style.height = `${Math.max(extent.height, 320)}px`;
    edgeLayer.setAttribute("width", inner.style.width);
    edgeLayer.setAttribute("height", inner.style.height);
  }

  function place(id) {
    const element = nodeElements.get(id);
    const position = positions[id];
    if (!element || !position) return;
    element.style.left = `${position.x}px`;
    element.style.top = `${position.y}px`;
    const port = portElements.get(id);
    if (port) {
      port.style.left = `${position.x + LAYOUT.nodeWidth / 2 - 46}px`;
      port.style.top = `${position.y + LAYOUT.nodeHeight - 11}px`;
    }
  }

  function edgeIsFlagged(edge) {
    return model.flaggedIds.has(edge.from) && model.flaggedIds.has(edge.to);
  }

  function drawEdges() {
    while (edgeGroup.firstChild) edgeGroup.removeChild(edgeGroup.firstChild);
    for (const edge of model.edges) {
      const from = positions[edge.from];
      const to = positions[edge.to];
      if (!from || !to) continue;
      const geometry = edgePath(from, to, LAYOUT);
      const selected = model.selectedEdgeId === edge.id;
      const flagged = edgeIsFlagged(edge);
      const group = svgEl("g", { class: `studio-edge-item${selected ? " selected" : ""}`, "data-edge-id": edge.id });
      const title = svgEl("title");
      title.textContent = edge.label || "connection";
      group.appendChild(title);
      const marker = selected ? "studio-arrow-selected" : flagged ? "studio-arrow-flag" : "studio-arrow";
      group.appendChild(
        svgEl("path", {
          d: geometry.d,
          class: `studio-edge${selected ? " selected" : ""}${flagged ? " flagged" : ""}${edge.done ? " done" : ""}`,
          "marker-end": `url(#${marker})`,
          fill: "none",
        })
      );
      const hit = svgEl("path", { d: geometry.d, class: "studio-edge-hit", fill: "none" });
      hit.addEventListener("click", (event) => {
        event.stopPropagation();
        if (handlers.onSelectEdge) handlers.onSelectEdge(edge.id);
      });
      group.appendChild(hit);
      edgeGroup.appendChild(group);
    }
  }

  function startDrag(event, id) {
    if (event.button !== 0 || (event.pointerType === "mouse" && event.buttons !== 1)) return;
    const startX = event.clientX;
    const startY = event.clientY;
    const origin = { ...positions[id] };
    let moved = false;
    const onMove = (moveEvent) => {
      const dx = moveEvent.clientX - startX;
      const dy = moveEvent.clientY - startY;
      if (!moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
      moved = true;
      positions[id] = { ...origin, x: Math.max(0, origin.x + dx), y: Math.max(0, origin.y + dy) };
      place(id);
      drawEdges();
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      if (!moved) return;
      suppressClick = true;
      setTimeout(() => {
        suppressClick = false;
      }, 0);
      size();
      if (handlers.onMove) handlers.onMove(id, positions[id].x, positions[id].y);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }

  function buildNode(node) {
    const isSelected = model.selectedNodeId === node.id;
    const isFlagged = model.flaggedIds.has(node.id);
    const isConnectSource = model.connectFromId === node.id;
    const isConnectTarget = Boolean(model.connectFromId) && !isConnectSource;
    const classes = ["studio-node", `studio-node-${node.type}`];
    if (node.status) classes.push(`run-state-${node.status.key}`);
    if (!draggable) classes.push("fixed");
    if (node.unsupported) classes.push("unsupported");
    if (isSelected) classes.push("selected");
    if (isFlagged) classes.push("flagged");
    if (node.hint) classes.push("needs-attention");
    if (isConnectSource) classes.push("connect-source");
    if (isConnectTarget) classes.push("connect-target");

    const children = [
      node.status ? el("span", { class: `run-pill run-pill-${node.status.key}` }, node.status.label) : null,
      el("span", { class: "studio-node-tag" }, node.tag),
      el("span", { class: "studio-node-title" }, node.title),
      ...node.lines.map((line) => el("span", { class: "studio-node-line" }, line)),
    ];
    if (node.hint) children.push(el("span", { class: "studio-node-hint" }, `⚠ ${node.hint}`));

    const button = el(
      "button",
      {
        type: "button",
        class: classes.join(" "),
        "data-node-id": node.id,
        "aria-pressed": isSelected ? "true" : "false",
        "aria-label": `${node.tag} ${node.title}${node.status ? `, ${node.status.label}` : ""}${node.hint ? `. ${node.hint}` : ""}`,
        style: `width:${LAYOUT.nodeWidth}px;height:${LAYOUT.nodeHeight}px`,
        onclick: (event) => {
          event.stopPropagation();
          if (suppressClick) return;
          if (model.connectFromId && model.editable && model.connectFromId !== node.id) {
            if (handlers.onConnect) handlers.onConnect(model.connectFromId, node.id);
            return;
          }
          if (handlers.onSelectNode) handlers.onSelectNode(node.id);
        },
      },
      children
    );
    if (draggable) button.addEventListener("pointerdown", (event) => startDrag(event, node.id));
    nodeElements.set(node.id, button);
    return button;
  }

  // The "connect" handle of the selected node: a real button (keyboard reachable)
  // beside the card, not inside it, so a button never contains another button.
  function buildPort(node) {
    const port = el(
      "button",
      {
        type: "button",
        class: "studio-port",
        title: "Connect this node to another",
        "aria-label": `Connect ${node.title} to another node`,
        onclick: (event) => {
          event.stopPropagation();
          if (handlers.onStartConnect) handlers.onStartConnect(node.id);
        },
      },
      model.connectFromId === node.id ? "Pick a target…" : "⤓ Connect"
    );
    portElements.set(node.id, port);
    return port;
  }

  wrapper.addEventListener("click", () => {
    if (handlers.onSelectNode) handlers.onSelectNode(null);
  });
  wrapper.addEventListener("keydown", (event) => {
    const tag = (event.target && event.target.tagName) || "";
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if (event.key === "Delete" || event.key === "Backspace") {
      if (handlers.onDelete) handlers.onDelete();
    } else if (event.key === "Escape") {
      if (handlers.onEscape) handlers.onEscape();
    }
  });

  function render(next) {
    model = { flaggedIds: new Set(), ...next };
    positions = {};
    for (const [id, position] of Object.entries(model.positions || {})) positions[id] = { x: position.x, y: position.y };
    for (const child of [...inner.children]) if (child !== edgeLayer) inner.removeChild(child);
    nodeElements.clear();
    portElements.clear();
    for (const node of model.nodes) {
      inner.appendChild(buildNode(node));
      if (model.selectedNodeId === node.id && model.editable && !node.unsupported) inner.appendChild(buildPort(node));
    }
    for (const id of nodeElements.keys()) place(id);
    size();
    drawEdges();
  }

  return { element: wrapper, render, focus: () => wrapper.focus() };
}
