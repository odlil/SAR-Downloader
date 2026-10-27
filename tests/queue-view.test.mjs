import test from "node:test";
import assert from "node:assert/strict";
import { createTaskListRenderer } from "../web/queue-view.js";

// Minimal DOM fixture: verify node identity and ordering, not rendered markup.
class Row {
  constructor(html) {
    this.html = html;
    this.parent = null;
  }
  get nextElementSibling() {
    return (
      this.parent?.children[this.parent.children.indexOf(this) + 1] || null
    );
  }
  contains() {
    return false;
  }
  remove() {
    if (this.parent) {
      this.parent.children.splice(this.parent.children.indexOf(this), 1);
      this.parent = null;
    }
  }
  replaceWith(node) {
    const parent = this.parent;
    const index = parent.children.indexOf(this);
    parent.children[index] = node;
    node.parent = parent;
    this.parent = null;
  }
}
function list() {
  return {
    children: [],
    get firstElementChild() {
      return this.children[0] || null;
    },
    insertBefore(node, before) {
      node.remove();
      const index = before
        ? this.children.indexOf(before)
        : this.children.length;
      this.children.splice(index, 0, node);
      node.parent = this;
    },
    ownerDocument: {
      activeElement: null,
      createElement() {
        return {
          content: {},
          set innerHTML(html) {
            this.content.firstElementChild = new Row(html);
          },
        };
      },
    },
  };
}
test("Polling preserves unchanged rows and updates only the changed task", () => {
  const container = list(),
    render = createTaskListRenderer(container);
  const markup = (row) => `<article>${row.id}:${row.state}</article>`;
  const tasks = [
    { id: "a", state: "queued" },
    { id: "b", state: "queued" },
  ];
  render(tasks, markup);
  const [a, b] = container.children;
  render(tasks, markup);
  assert.equal(container.children[0], a);
  assert.equal(container.children[1], b);
  render([{ ...tasks[0], state: "paused" }, tasks[1]], markup);
  assert.notEqual(container.children[0], a);
  assert.equal(container.children[1], b);
  render([tasks[1]], markup);
  assert.deepEqual(container.children, [b]);
  render([tasks[1], tasks[0]], markup);
  assert.equal(container.children[0], b);
});
