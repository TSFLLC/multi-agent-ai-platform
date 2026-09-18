// Pure candidate-index navigation for the Comparison Results Focus View
// (MA5-UI Final Readability Enhancement) -- no DOM, unit-tested directly
// (see frontend/tests/focusNav.test.mjs).
//
// Chosen boundary behavior: CLAMP, never wrap. Previous at the first
// candidate and Next at the last are simply unavailable (the caller
// disables those buttons via hasPrevious/hasNext) rather than cycling
// around to the other end -- the simpler, more predictable choice for an
// operator reading through a short, fixed list of candidates.

export function clampIndex(index, length) {
  if (length <= 0) return 0;
  if (index < 0) return 0;
  if (index >= length) return length - 1;
  return index;
}

export function hasPrevious(index) {
  return index > 0;
}

export function hasNext(index, length) {
  return index < length - 1;
}

export function previousIndex(index) {
  return index - 1;
}

export function nextIndex(index) {
  return index + 1;
}
