// DEM tasks have a real `plan` object. Only explicit legacy rows are previews.
export function queueRows(tasks, legacy) {
  const ids = new Set(tasks.map((task) => task.id));
  return [
    ...tasks.map((task) => ({ ...task, legacyPlan: false })),
    ...legacy
      .filter((task) => !ids.has(task.id))
      .map((task) => ({ ...task, legacyPlan: true })),
  ];
}

export function createTaskListRenderer(container) {
  const previous = new Map();
  return function render(rows, renderRow) {
    const wanted = new Set(rows.map((row) => row.id));
    for (const [key, entry] of previous) {
      if (!wanted.has(key)) {
        entry.node.remove();
        previous.delete(key);
      }
    }
    let cursor = container.firstElementChild;
    for (const row of rows) {
      const html = renderRow(row);
      let entry = previous.get(row.id);
      if (!entry || entry.html !== html) {
        const template = container.ownerDocument.createElement("template");
        template.innerHTML = html;
        const node = template.content.firstElementChild;
        if (entry) {
          const wasCursor = entry.node === cursor;
          const active = container.ownerDocument.activeElement;
          const action = entry.node.contains(active)
            ? active?.dataset.taskAction
            : null;
          entry.node.replaceWith(node);
          if (wasCursor) cursor = node;
          if (action)
            node
              .querySelector(`[data-task-action="${action}"]`)
              ?.focus({ preventScroll: true });
        }
        entry = { html, node };
        previous.set(row.id, entry);
      }
      if (entry.node !== cursor) container.insertBefore(entry.node, cursor);
      cursor = entry.node.nextElementSibling;
    }
  };
}

export function pollingDelay(tasks, hidden = false) {
  if (hidden) return 15000;
  return tasks.some(
    (task) =>
      task.busy ||
      ["downloading", "waiting", "retrying", "processing"].includes(task.state),
  )
    ? 1500
    : 5000;
}
