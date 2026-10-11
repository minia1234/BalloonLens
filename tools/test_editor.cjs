const { test } = require('node:test');
const assert = require('node:assert/strict');
const E = require('../extension/editor.js');

const snap = {
  width: 400, height: 300,
  layout: { removed: [], added: [], frames: {} },
  segments: [
    { id: 0, bbox: [300, 10, 20, 80], src: '右', origin: { kind: 'ocr', key: '300,10,20,80' },
      presentation: { kind: 'bubble', frame: [280, 10, 60, 90] } },
    { id: 1, bbox: [100, 10, 20, 80], src: '左', origin: { kind: 'ocr', key: '100,10,20,80' } },
  ],
};

test('snapshot keeps automatic and reader frames apart', () => {
  const s = E.fromSnapshot({ ...snap, segments: [snap.segments[0], { ...snap.segments[1], presentation: { frame: [90, 5, 40, 40], fill: true } }] });
  assert.deepEqual(s.regions[0].auto, [280, 10, 60, 90]);
  assert.equal(s.regions[0].frame, null);
  assert.deepEqual(s.regions[1].frame, [90, 5, 40, 40]);
  assert.equal(s.regions[1].auto, null);
});

test('moving a detected box removes the old key and adds a reader region that keeps its frame', () => {
  let s = E.fromSnapshot(snap);
  s = E.setFrame(s, 0, [270, 10, 70, 60]);
  assert.deepEqual(s.layout.frames, { '300,10,20,80': [270, 10, 70, 60] });
  s = E.setBox(s, 0, [296, 12, 26, 90]);
  assert.deepEqual(s.layout.removed, ['300,10,20,80']);
  assert.deepEqual(s.layout.frames, {});
  assert.equal(s.layout.added.length, 1);
  assert.deepEqual(s.layout.added[0].bbox, [296, 12, 26, 90]);
  assert.deepEqual(s.layout.added[0].frame, [270, 10, 70, 60]);
  assert.equal(s.regions[0].ref.kind, 'user');
  // Moving it again updates the same reader region.
  s = E.setBox(s, 0, [290, 12, 26, 90]);
  assert.equal(s.layout.added.length, 1);
  assert.deepEqual(s.layout.added[0].bbox, [290, 12, 26, 90]);
});

test('moved regions tell the helper where their text fixes were stored', () => {
  let s = E.setBox(E.fromSnapshot(snap), 0, [296, 12, 26, 90]);
  assert.equal(s.layout.added[0].moved_from, '300,10,20,80');
  s = E.setBox(s, 0, [290, 12, 26, 90]);
  assert.equal(s.layout.added[0].moved_from, '300,10,20,80');
  // After saving, the region comes back as a reader region; an old moved_from is not replayed.
  const saved = E.fromSnapshot({
    ...snap,
    layout: { removed: ['300,10,20,80'], added: [{ uid: 'u1', bbox: [290, 12, 26, 90], frame: null, moved_from: '300,10,20,80' }], frames: {} },
    segments: [{ id: 0, bbox: [290, 12, 26, 90], src: '右', origin: { kind: 'user', uid: 'u1' } }],
  });
  assert.equal(saved.layout.added[0].moved_from, undefined);
  assert.equal(E.setFrame(saved, 0, [1, 1, 20, 20]).layout.added[0].moved_from, undefined);
  assert.equal(E.setBox(saved, 0, [280, 12, 26, 90]).layout.added[0].moved_from, '290,12,26,90');
  // Split/merge results are new text: nothing to carry.
  assert.ok(E.split(E.fromSnapshot(snap), 0, 'y').layout.added.every((a) => !a.moved_from));
});

test('operations never mutate the previous state (undo relies on it)', () => {
  const s0 = E.fromSnapshot(snap);
  const before = JSON.stringify(s0);
  E.remove(E.split(E.setFrame(s0, 1, [1, 1, 20, 20]), 0, 'x'), [0]);
  assert.equal(JSON.stringify(s0), before);
});

test('split and merge', () => {
  let s = E.split(E.fromSnapshot(snap), 0, 'y');
  assert.deepEqual(s.layout.removed, ['300,10,20,80']);
  assert.deepEqual(s.layout.added.map((a) => a.bbox), [[300, 10, 20, 40], [300, 50, 20, 40]]);
  assert.equal(s.regions.length, 3);
  // Two reader regions merged: both drop out of `added`, a union replaces them.
  s = E.merge(s, [1, 2]);
  assert.deepEqual(s.layout.added.map((a) => a.bbox), [[300, 10, 20, 80]]);
  // Merging a detected and a reader region.
  s = E.merge(s, [0, 1]);
  assert.deepEqual(s.layout.removed.sort(), ['100,10,20,80', '300,10,20,80']);
  assert.deepEqual(s.layout.added.map((a) => a.bbox), [[100, 10, 220, 80]]);
  // Too small to split: unchanged.
  const tiny = E.add(E.fromSnapshot({ ...snap, segments: [] }), [0, 0, 10, 10]);
  assert.equal(E.split(tiny, 0, 'x'), tiny);
});

test('deleting a reader region only drops it from added; resetting a frame clears it', () => {
  let s = E.add(E.fromSnapshot(snap), [10, 10, 30, 30]);
  s = E.setFrame(s, 2, [5, 5, 50, 50]);
  assert.deepEqual(s.layout.added[0].frame, [5, 5, 50, 50]);
  s = E.setFrame(s, 2, null);
  assert.equal(s.layout.added[0].frame, null);
  s = E.remove(s, [2]);
  assert.deepEqual(s.layout, { removed: [], added: [], frames: {} });
});

test('clamp keeps boxes inside the image and at least 8px', () => {
  assert.deepEqual(E.clampBox([-5, 290, 2, 40], 400, 300), [0, 260, 8, 40]);
  assert.deepEqual(E.clampBox([390, 0, 50, 50], 400, 300), [350, 0, 50, 50]);
});

test('history undo/redo', () => {
  const h = E.history(1);
  h.push(2); h.push(3);
  assert.equal(h.undo(), 2);
  assert.equal(h.canRedo, true);
  h.push(4);
  assert.equal(h.canRedo, false);
  assert.equal(h.undo(), 2);
  assert.equal(h.redo(), 4);
});
