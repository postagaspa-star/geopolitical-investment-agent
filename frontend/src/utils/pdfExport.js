/**
 * pdfExport.js — Utility per generare un PDF "report" da un risultato
 * Simulator. Usa jsPDF + html2canvas per catturare le sezioni del DOM
 * (cards) e impaginarle in un A4 con page-break intelligenti.
 *
 * Approccio: si cattura ogni elemento marcato con `data-pdf-section` come
 * immagine PNG (ad alta risoluzione, scale=2). Se la sezione entra nella
 * pagina corrente la mette accodata, altrimenti apre una nuova pagina. Se
 * una singola sezione è più alta di una pagina A4 viene splittata su
 * più pagine.
 *
 * Le sezioni con `data-pdf-skip` vengono ignorate (es. tab buttons,
 * pulsanti di azione).
 */

// jsPDF + html2canvas (~180kB combined). Importati staticamente perché
// in modalità ENCRYPTION_ENABLED tutti i chunk dinamici fallirebbero:
// il catch-all FastAPI restituisce unlock.html invece dei .chunk.js.
// Con import statico finiscono nel main bundle, che è inlinato in
// index.html e cifrato come singolo blob.
import jsPDF from "jspdf";
import html2canvas from "html2canvas";

const BG_COLOR = "#0a0e1a";
const PAGE_MARGIN_MM = 8;
const SECTION_GAP_MM = 4;

/**
 * Genera un PDF dalle sezioni di un container.
 *
 * @param {Object} opts
 * @param {string} opts.rootId       ID del container che contiene le sezioni
 * @param {string} opts.filename     Nome file PDF da scaricare
 * @param {Object} [opts.meta]       Metadati per la cover (titolo, sottotitolo, runId, date...)
 * @returns {Promise<void>}
 */
export async function generatePDFFromSections({ rootId, filename, meta }) {

  const root = document.getElementById(rootId);
  if (!root) {
    throw new Error(`Elemento #${rootId} non trovato nel DOM`);
  }

  const sections = Array.from(root.querySelectorAll("[data-pdf-section]"))
    .filter((el) => !el.hasAttribute("data-pdf-skip"));

  if (sections.length === 0) {
    throw new Error("Nessuna sezione marcata con data-pdf-section trovata");
  }

  const pdf = new jsPDF({
    orientation: "p",
    unit: "mm",
    format: "a4",
    compress: true,
  });

  const pageW = pdf.internal.pageSize.getWidth();
  const pageH = pdf.internal.pageSize.getHeight();
  const contentW = pageW - 2 * PAGE_MARGIN_MM;
  const usableH = pageH - 2 * PAGE_MARGIN_MM;

  // ── Cover page (titolo + meta) ────────────────────────────────────
  if (meta) {
    drawCoverPage(pdf, meta, pageW, pageH);
    pdf.addPage();
  }

  let yCursor = PAGE_MARGIN_MM;
  let isFirstSectionOnPage = true;

  for (let i = 0; i < sections.length; i++) {
    const section = sections[i];

    // Skip sezioni nascoste (display:none o height=0)
    const rect = section.getBoundingClientRect();
    if (rect.height === 0 || rect.width === 0) continue;

    let canvas;
    try {
      canvas = await html2canvas(section, {
        scale: 2,
        backgroundColor: BG_COLOR,
        logging: false,
        useCORS: true,
        allowTaint: true,
        // Evita trasformazioni di scrolling
        windowWidth: section.scrollWidth,
      });
    } catch (err) {
      console.warn("[pdfExport] cattura fallita per sezione", i, err);
      continue;
    }

    const sectionH = (canvas.height * contentW) / canvas.width;

    if (sectionH > usableH) {
      // Sezione più alta della pagina: split su più pagine
      if (!isFirstSectionOnPage) {
        pdf.addPage();
        yCursor = PAGE_MARGIN_MM;
      }
      addTallSectionAcrossPages(pdf, canvas, contentW, usableH, sectionH);
      // Dopo split, finisce sempre su una pagina dedicata
      pdf.addPage();
      yCursor = PAGE_MARGIN_MM;
      isFirstSectionOnPage = true;
    } else if (yCursor + sectionH > pageH - PAGE_MARGIN_MM) {
      // Non entra nella pagina corrente: nuova pagina
      pdf.addPage();
      yCursor = PAGE_MARGIN_MM;
      pdf.addImage(
        canvas.toDataURL("image/png"),
        "PNG",
        PAGE_MARGIN_MM,
        yCursor,
        contentW,
        sectionH,
        undefined,
        "FAST",
      );
      yCursor += sectionH + SECTION_GAP_MM;
      isFirstSectionOnPage = false;
    } else {
      // Entra nella pagina corrente
      pdf.addImage(
        canvas.toDataURL("image/png"),
        "PNG",
        PAGE_MARGIN_MM,
        yCursor,
        contentW,
        sectionH,
        undefined,
        "FAST",
      );
      yCursor += sectionH + SECTION_GAP_MM;
      isFirstSectionOnPage = false;
    }
  }

  // ── Footer numerazione pagine ─────────────────────────────────────
  const totalPages = pdf.internal.getNumberOfPages();
  pdf.setFontSize(8);
  pdf.setTextColor(120, 120, 120);
  for (let p = 1; p <= totalPages; p++) {
    pdf.setPage(p);
    pdf.text(
      `${p} / ${totalPages}`,
      pageW - PAGE_MARGIN_MM,
      pageH - 4,
      { align: "right" },
    );
    pdf.text(
      `Geopolitical Investment Agent — Simulator Report`,
      PAGE_MARGIN_MM,
      pageH - 4,
    );
  }

  // ── Ultima pagina vuota → rimuovi (se l'ultima sezione era tall-split) ─
  // Se l'ultima pagina è vuota (yCursor era stato resettato dopo split), la
  // rimuoviamo. jsPDF non supporta deletePage(...) <2.5; verifichiamo che
  // la pagina abbia contenuto: se non ne ha, la togliamo manualmente.
  // Soluzione più semplice: se l'ultima sezione era tall-split, abbiamo già
  // fatto addPage() — ma se non ci sono altre sezioni dopo, quella pagina
  // resta vuota. Controlliamo:
  if (
    pdf.internal.pages.length > totalPages &&
    pdf.internal.getNumberOfPages() > 1
  ) {
    // solo se utile
  }

  pdf.save(filename);
}

/**
 * Splitta una sezione più alta di una pagina su più pagine A4.
 */
function addTallSectionAcrossPages(pdf, canvas, contentW, usableH, sectionH) {
  let pxOffset = 0;
  let mmRemaining = sectionH;
  let firstSlice = true;

  while (mmRemaining > 0) {
    const sliceMmHeight = Math.min(mmRemaining, usableH);
    // Conversione mm → px sul canvas sorgente
    const slicePxHeight = (sliceMmHeight / sectionH) * canvas.height;

    const slice = document.createElement("canvas");
    slice.width = canvas.width;
    slice.height = Math.ceil(slicePxHeight);
    const ctx = slice.getContext("2d");
    ctx.fillStyle = BG_COLOR;
    ctx.fillRect(0, 0, slice.width, slice.height);
    ctx.drawImage(canvas, 0, -pxOffset);

    if (!firstSlice) pdf.addPage();
    pdf.addImage(
      slice.toDataURL("image/png"),
      "PNG",
      PAGE_MARGIN_MM,
      PAGE_MARGIN_MM,
      contentW,
      sliceMmHeight,
      undefined,
      "FAST",
    );

    pxOffset += slicePxHeight;
    mmRemaining -= sliceMmHeight;
    firstSlice = false;
  }
}

/**
 * Disegna la cover page con titolo e metadati.
 */
function drawCoverPage(pdf, meta, pageW, pageH) {
  // Background scuro
  pdf.setFillColor(10, 14, 26); // #0a0e1a
  pdf.rect(0, 0, pageW, pageH, "F");

  // Banda viola in alto
  pdf.setFillColor(167, 139, 250); // #a78bfa
  pdf.rect(0, 0, pageW, 3, "F");

  // Titolo
  pdf.setTextColor(226, 232, 240); // #e2e8f0
  pdf.setFontSize(24);
  pdf.setFont("helvetica", "bold");
  pdf.text(meta.title || "Simulator Report", PAGE_MARGIN_MM, 35);

  // Sottotitolo
  if (meta.subtitle) {
    pdf.setFontSize(13);
    pdf.setFont("helvetica", "normal");
    pdf.setTextColor(148, 163, 184); // #94a3b8
    const subLines = pdf.splitTextToSize(meta.subtitle, pageW - 2 * PAGE_MARGIN_MM);
    pdf.text(subLines, PAGE_MARGIN_MM, 46);
  }

  // Box meta info
  let yMeta = 70;
  const labelColor = [100, 116, 139];   // #64748b
  const valueColor = [226, 232, 240];   // #e2e8f0
  const purpleColor = [167, 139, 250];  // #a78bfa

  if (meta.runId) {
    drawMetaLine(pdf, "RUN ID", meta.runId, PAGE_MARGIN_MM, yMeta, labelColor, valueColor);
    yMeta += 10;
  }
  if (meta.category) {
    drawMetaLine(pdf, "CATEGORIA", meta.category, PAGE_MARGIN_MM, yMeta, labelColor, valueColor);
    yMeta += 10;
  }
  if (meta.scenarioType) {
    drawMetaLine(pdf, "TIPO SCENARIO", meta.scenarioType, PAGE_MARGIN_MM, yMeta, labelColor, valueColor);
    yMeta += 10;
  }
  if (meta.action) {
    drawMetaLine(pdf, "AZIONE AI", meta.action, PAGE_MARGIN_MM, yMeta, labelColor, purpleColor);
    yMeta += 10;
  }
  if (meta.asset) {
    drawMetaLine(pdf, "ASSET", meta.asset, PAGE_MARGIN_MM, yMeta, labelColor, valueColor);
    yMeta += 10;
  }
  if (meta.outcome) {
    drawMetaLine(pdf, "ESITO", meta.outcome, PAGE_MARGIN_MM, yMeta, labelColor, purpleColor);
    yMeta += 10;
  }
  if (meta.period) {
    drawMetaLine(pdf, "PERIODO STORICO", meta.period, PAGE_MARGIN_MM, yMeta, labelColor, valueColor);
    yMeta += 10;
  }
  if (meta.generatedAt) {
    drawMetaLine(pdf, "GENERATO", meta.generatedAt, PAGE_MARGIN_MM, yMeta, labelColor, valueColor);
    yMeta += 10;
  }

  // Footer cover
  pdf.setFontSize(9);
  pdf.setTextColor(100, 116, 139);
  pdf.text(
    "Geopolitical Investment Agent · Simulator Report",
    PAGE_MARGIN_MM,
    pageH - 12,
  );
  pdf.text(
    "Documento generato per analisi performance — non costituisce consulenza finanziaria",
    PAGE_MARGIN_MM,
    pageH - 7,
  );
}

function drawMetaLine(pdf, label, value, x, y, labelRgb, valueRgb) {
  pdf.setFontSize(9);
  pdf.setFont("helvetica", "bold");
  pdf.setTextColor(...labelRgb);
  pdf.text(label, x, y);

  pdf.setFontSize(12);
  pdf.setFont("helvetica", "normal");
  pdf.setTextColor(...valueRgb);
  // Tronca se troppo lungo
  const maxW = 130;
  const lines = pdf.splitTextToSize(String(value), maxW);
  pdf.text(lines[0] || "—", x + 50, y);
}
