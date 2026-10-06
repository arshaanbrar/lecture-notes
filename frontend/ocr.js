"use strict";

// Read scanned PDFs and photos on this device instead of on the server. The free server has a
// small slice of one CPU, so reading scans there is slow; a laptop or phone is many times faster.
// pdf.js finds the pages that have no text, and Tesseract.js (the same OCR engine the server uses,
// compiled for the browser) reads them. Both load only when a scan is actually picked, and the
// browser caches them (about 4 MB the first time). If anything fails, the file is uploaded and
// read on the server as before.

const OCR = {
  pdfjs: "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js",
  pdfWorker: "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js",
  tesseract: "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js",
  tesseractWorker: "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/worker.min.js",
  tesseractCore: "https://cdn.jsdelivr.net/npm/tesseract.js-core@5.1.1",
  lang: "https://cdn.jsdelivr.net/npm/@tesseract.js-data/eng@1.0.0/4.0.0_best_int",
  maxPages: 150,      // scanned pages read on the device (the server reads at most 40)
  minPageChars: 25,   // a page with less text than this is probably a scan (same as the server)
  scale: 150 / 72,    // render at 150 dpi, like the server
};

const scansOnDevice = /\.(pdf|png|jpe?g)$/i;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) return resolve();
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`Couldn't load ${src}`));
    document.head.append(s);
  });
}

async function ocrWorkers() {
  await loadScript(OCR.tesseract);
  // Two at a time on computers with a few cores; one on smaller devices.
  const count = (navigator.hardwareConcurrency || 2) >= 4 ? 2 : 1;
  const scheduler = Tesseract.createScheduler();
  for (let i = 0; i < count; i++) {
    const worker = await Tesseract.createWorker("eng", 1, {
      workerPath: OCR.tesseractWorker, corePath: OCR.tesseractCore, langPath: OCR.lang,
    });
    scheduler.addWorker(worker);
  }
  return { scheduler, count };
}

// The text of a scanned PDF or photo, read on this device. Returns null when there's nothing to
// gain (a PDF that already has its text: the server reads that in a moment).
async function readScanOnDevice(file, progress) {
  if (!scansOnDevice.test(file.name || "")) return null;
  if (/\.(png|jpe?g)$/i.test(file.name)) {
    progress("Reading the text in the photo on this device…");
    const { scheduler } = await ocrWorkers();
    try {
      const { data } = await scheduler.addJob("recognize", file);
      return data.text.trim() || null;
    } finally {
      scheduler.terminate();
    }
  }

  await loadScript(OCR.pdfjs);
  pdfjsLib.GlobalWorkerOptions.workerSrc = OCR.pdfWorker;
  const pdf = await pdfjsLib.getDocument({ data: await file.arrayBuffer() }).promise;
  const pages = [];
  for (let n = 1; n <= Math.min(pdf.numPages, 500); n++) {
    const content = await (await pdf.getPage(n)).getTextContent();
    pages.push(content.items.map((item) => item.str).join(" ").trim());
  }
  const missing = pages.map((t, i) => (t.length < OCR.minPageChars ? i : -1)).filter((i) => i >= 0);
  if (!missing.length) return null; // a normal PDF: no scanned pages
  const reading = missing.slice(0, OCR.maxPages);

  progress("Getting the text reader ready…");
  const { scheduler, count } = await ocrWorkers();
  let done = 0, next = 0;
  // Each reader takes the next page when it's free, so only a page or two is in memory at once.
  const reader = async () => {
    while (next < reading.length) {
      const i = reading[next++];
      const page = await pdf.getPage(i + 1);
      const viewport = page.getViewport({ scale: OCR.scale });
      const canvas = document.createElement("canvas");
      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      // "print" draws without waiting for animation frames, which browsers pause in background tabs.
      await page.render({ canvasContext: canvas.getContext("2d"), viewport, intent: "print" }).promise;
      const { data } = await scheduler.addJob("recognize", canvas);
      canvas.width = canvas.height = 0; // free the memory
      if (data.text.trim().length > pages[i].length) pages[i] = data.text.trim();
      progress(`Reading scanned pages on this device… ${++done} of ${reading.length}`);
    }
  };
  try {
    await Promise.all(Array.from({ length: count }, reader));
  } finally {
    scheduler.terminate();
    pdf.destroy();
  }
  const text = pages.filter(Boolean).join("\n\n");
  return text.trim() ? text : null;
}
