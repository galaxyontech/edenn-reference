/* Shared presentation helpers. Audio peaks come from the asset, never a fixture pattern. */
(function () {
  "use strict";
  const peakCache = new Map();
  const MAX_BYTES = 12 * 1024 * 1024;
  async function readPeaks(url) {
    const response = await fetch(url);
    if (!response.ok || Number(response.headers.get("content-length")) > MAX_BYTES) return null;
    const reader = response.body.getReader();
    const chunks = [];
    let length = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > MAX_BYTES) { await reader.cancel(); return null; }
      chunks.push(value);
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    chunks.forEach((chunk) => { bytes.set(chunk, offset); offset += chunk.length; });
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return null;
    const context = new Context();
    try {
      const audio = await context.decodeAudioData(bytes.buffer);
      const peaks = new Float32Array(1024);
      for (let channel = 0; channel < audio.numberOfChannels; channel++) {
        const samples = audio.getChannelData(channel);
        const stride = Math.max(1, Math.ceil(samples.length / peaks.length));
        for (let i = 0; i < samples.length; i++) {
          const bucket = Math.floor(i / stride);
          peaks[bucket] = Math.max(peaks[bucket], Math.abs(samples[i]));
        }
      }
      const loudest = Math.max(...peaks);
      // Scale relative to this asset so quiet recordings remain readable.
      // Preserve silence instead of turning decoding noise into a waveform.
      if (loudest > 0.0001) for (let i = 0; i < peaks.length; i++) peaks[i] /= loudest;
      return peaks;
    } finally { await context.close(); }
  }
  function peaksFor(url) {
    if (!peakCache.has(url)) {
      if (peakCache.size >= 24) peakCache.delete(peakCache.keys().next().value);
      peakCache.set(url, readPeaks(url).catch(() => null));
    }
    return peakCache.get(url);
  }
  function waveform(node, url) {
    node.setAttribute("aria-hidden", "true");
    if (!url) return;
    peaksFor(url).then((peaks) => {
      if (!peaks || !node.isConnected) return;
      node._peaks = peaks;
      drawWave(node);
      waveResize.observe(node);
    });
  }
  function drawWave(node) {
    const width = Math.round(node.getBoundingClientRect().width);
    const count = Math.max(1, Math.floor(width / 4));
    if (!width || node._count === count) return;
    node._count = count;
    const peaks = node._peaks;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} 24`);
    svg.setAttribute("preserveAspectRatio", "none");
    let path = "";
    for (let i = 0; i < count; i++) {
      const from = Math.floor(i * peaks.length / count);
      const to = Math.max(from + 1, Math.floor((i + 1) * peaks.length / count));
      let peak = 0;
      for (let j = from; j < to; j++) peak = Math.max(peak, peaks[j]);
      const height = Math.max(0.6, peak * 21);
      path += `M${i * 4 + 1} ${12 - height / 2}v${height} `;
    }
    const line = document.createElementNS(svg.namespaceURI, "path");
    line.setAttribute("d", path);
    svg.appendChild(line);
    node.replaceChildren(svg);
  }
  const waveResize = new ResizeObserver((entries) => entries.forEach(({ target }) => {
    if (!target.isConnected) { waveResize.unobserve(target); return; }
    drawWave(target);
  }));
  // Removed media nodes must not be retained by the resize observer.
  new MutationObserver((records) => records.forEach((record) => record.removedNodes.forEach((node) => {
    if (node.nodeType !== 1) return;
    if (node._peaks && !node.isConnected) waveResize.unobserve(node);
    node.querySelectorAll(".take__wave, .tl-clip__wave").forEach((wave) => {
      if (!wave.isConnected) waveResize.unobserve(wave);
    });
  }))).observe(document.body, { childList: true, subtree: true });

  window.EdennUI = { waveform, reducedMotion: () => matchMedia("(prefers-reduced-motion: reduce)").matches };
})();
