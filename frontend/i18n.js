/* Interface language.
 *
 * A Government of India console has to work in Hindi as well as English, so the
 * chrome is translated: navigation, headings, buttons, help text, the wording of
 * findings.
 *
 * What is deliberately NOT translated:
 *
 *   - IS numbers and the official titles of Indian Standards. Those are the
 *     register's own text. "Crosslinked polyethylene insulated thermoplastics
 *     sheathed cables" is the standard's legal title in the BIS catalogue, and
 *     rendering an unofficial Hindi paraphrase of it in a procurement document
 *     would be inventing official text. They stay verbatim in every language.
 *   - Certification scheme names and Gazette notification references, for the
 *     same reason: they are citations to legal instruments.
 *   - Tender IDs, file names, source links.
 *
 * So a Hindi user sees a Hindi interface wrapped around English standard titles.
 * That is exactly how BIS's own portal behaves, and it is the honest arrangement:
 * the interface is ours to translate, the register is not.
 *
 * Adding a language is a data change, not a code change: add a block to STRINGS
 * with the same keys and an entry to LANGS. Missing keys fall back to English
 * rather than showing a blank, so a partial translation is still usable.
 */

/* Native names for generated locales, so a new language shows in its own script
   in the switcher rather than as a bare code. */
const LOCALE_NAMES = {
  hi: 'हिन्दी', bn: 'বাংলা', mr: 'मराठी', te: 'తెలుగు', ta: 'தமிழ்', gu: 'ગુજરાતી',
  kn: 'ಕನ್ನಡ', ml: 'മലയാളം', pa: 'ਪੰਜਾਬੀ', or: 'ଓଡ଼ିଆ', as: 'অসমীয়া', ur: 'اردو',
  sa: 'संस्कृतम्', ne: 'नेपाली', kok: 'कोंकणी', mai: 'मैथिली', doi: 'डोगरी',
  brx: 'बड़ो', sat: 'ᱥᱟᱱᱛᱟᱲᱤ', mni: 'ꯃꯤꯇꯩꯂꯣꯟ', ks: 'کٲشُر', sd: 'سنڌي',
};

/* English and Hindi carry hand-written strings below. The rest rely on the
   page translator, which falls back to English for any key and translates the
   rendered result — so adding a language costs one line here, and a reviewed
   block in STRINGS later improves it without changing any code. */
const LANGS = [
  { code: 'en', label: 'English',  native: 'English' },
  { code: 'hi', label: 'Hindi',    native: 'हिन्दी' },
  { code: 'mr', label: 'Marathi',  native: 'मराठी' },
  { code: 'bn', label: 'Bengali',  native: 'বাংলা' },
  { code: 'ta', label: 'Tamil',    native: 'தமிழ்' },
  { code: 'te', label: 'Telugu',   native: 'తెలుగు' },
  { code: 'gu', label: 'Gujarati', native: 'ગુજરાતી' },
  { code: 'kn', label: 'Kannada',  native: 'ಕನ್ನಡ' },
  { code: 'ml', label: 'Malayalam', native: 'മലയാളം' },
  { code: 'pa', label: 'Punjabi',  native: 'ਪੰਜਾਬੀ' },
  { code: 'or', label: 'Odia',     native: 'ଓଡ଼ିଆ' },
  { code: 'ur', label: 'Urdu',     native: 'اردو' },
];

const STRINGS = {
  en: {
    'app.tagline': 'Standards verification',

    'nav.draft': 'Draft', 'nav.analyze': 'Audit', 'nav.decisions': 'Decisions',
    'nav.evidence': 'Evidence', 'nav.overview': 'Overview', 'nav.tenders': 'Tenders',
    'nav.standards': 'Standards', 'nav.certs': 'Certification', 'nav.graph': 'Graph',
    'nav.coverage': 'Coverage', 'nav.benchmark': 'Benchmark',

    'full.draft': 'Draft clause', 'full.analyze': 'Tender audit',
    'full.decisions': 'Officer decisions', 'full.evidence': 'Corpus evidence',
    'full.overview': 'Overview', 'full.tenders': 'Tender corpus',
    'full.standards': 'Standards register', 'full.certs': 'Certification duties',
    'full.graph': 'Co-citation graph', 'full.coverage': 'Coverage',
    'full.benchmark': 'Detection benchmark',

    'ui.console': 'Console', 'ui.search': 'Search', 'ui.runDemo': 'Run demo',
    'ui.reset': 'Reset', 'ui.report': 'Report', 'ui.refresh': 'Refresh',
    'ui.officer': 'OFFICER', 'ui.admin': 'ADMIN', 'ui.language': 'Language',
    'ui.connected': 'connected', 'ui.offline': 'backend offline',
    'ui.tables': 'tables', 'ui.data': 'data',

    'draft.meta': 'hybrid retrieval · BM25 + MiniLM → RRF → cross-encoder → confidence gate',
    'draft.specText': 'Specification text', 'draft.whatOfficer': 'what the officer wants to buy',
    'draft.find': 'Find governing standard', 'draft.governing': 'Governing standard',
    'draft.certDuty': 'Certification duty', 'draft.coCited': 'Co-cited standards',
    'draft.clause': 'Composed clause', 'draft.copyClause': 'Copy clause',
    'draft.trace': 'Retrieval trace', 'draft.everyCandidate': 'every candidate the gate saw',
    'draft.noRule': 'No rule on file for this standard.',

    'audit.meta': 'input: PDF, clause text, or IS numbers',
    'audit.step1': 'Step 1', 'audit.step2': 'Step 2',
    'audit.source': 'Source document', 'audit.citations': 'Citations to verify',
    'audit.drop': 'Drop a tender PDF or .docx, or click to browse',
    'audit.dropHint': 'Parsed server-side · 25 MB · first 60 pages · scans are detected, not silently passed',
    'audit.orPaste': 'or paste specification text',
    'audit.extract': 'Extract IS numbers', 'audit.loadCorpus': 'Load from corpus',
    'audit.run': 'Run verification', 'audit.add': 'Add',
    'audit.queued': 'queued',

    'find.dispute_risk': 'Dispute risk', 'find.statutory_omission': 'Statutory omission',
    'find.missing_connected': 'Missing standard', 'find.not_in_register': 'Not in register',
    'find.action': 'Action.', 'find.accept': 'Accept', 'find.override': 'Override…',
    'find.accepted': 'Accepted', 'find.overridden': 'Overridden',
    'find.source': 'source', 'find.seeCiting': 'see citing tenders',

    'verdict.blocking': 'Blocking — do not publish as written',
    'verdict.review': 'Review recommended',
    'verdict.clean': 'Nothing flagged',
    'verdict.unreadable': 'Document could not be read',

    'dec.log': 'Audit log', 'dec.newest': 'newest first', 'dec.when': 'When',
    'dec.officer': 'Officer', 'dec.standard': 'Standard', 'dec.finding': 'Finding',
    'dec.verdict': 'Verdict', 'dec.reason': 'Reason', 'dec.mix': 'Verdict mix',
    'dec.logged': 'Decisions logged', 'dec.agreement': 'Agreement rate',

    'ev.meta': 'which published tenders cite a standard',
    'ev.lookup': 'Look up a standard', 'ev.show': 'Show evidence',
    'ev.everyRow': 'every row links to the source document',
    'ev.citing': 'Citing tenders', 'ev.tendersCiting': 'Tenders citing',
    'ev.share': 'Share of corpus', 'ev.inRegister': 'In the register',

    'msg.backendDown': 'Backend unreachable.',
    'msg.nothingYet': 'Nothing verified yet',
    'msg.queueFirst': 'Queue some citations and run verification.',
    'msg.reqCitation': 'Add a citation or some clause text first',
    'msg.reqSpec': 'Enter some specification text first',
    'msg.titlesEnglish': 'Standard titles are shown in English as published by BIS.',
  },

  hi: {
    'app.tagline': 'मानक सत्यापन',

    'nav.draft': 'प्रारूप', 'nav.analyze': 'लेखा-परीक्षा', 'nav.decisions': 'निर्णय',
    'nav.evidence': 'प्रमाण', 'nav.overview': 'सारांश', 'nav.tenders': 'निविदाएँ',
    'nav.standards': 'मानक', 'nav.certs': 'प्रमाणन', 'nav.graph': 'ग्राफ़',
    'nav.coverage': 'कवरेज', 'nav.benchmark': 'मानदंड',

    'full.draft': 'खंड प्रारूप', 'full.analyze': 'निविदा लेखा-परीक्षा',
    'full.decisions': 'अधिकारी के निर्णय', 'full.evidence': 'संग्रह प्रमाण',
    'full.overview': 'सारांश', 'full.tenders': 'निविदा संग्रह',
    'full.standards': 'मानक रजिस्टर', 'full.certs': 'प्रमाणन दायित्व',
    'full.graph': 'सह-उद्धरण ग्राफ़', 'full.coverage': 'कवरेज',
    'full.benchmark': 'पहचान मानदंड',

    'ui.console': 'कंसोल', 'ui.search': 'खोजें', 'ui.runDemo': 'डेमो चलाएँ',
    'ui.reset': 'रीसेट', 'ui.report': 'रिपोर्ट', 'ui.refresh': 'ताज़ा करें',
    'ui.officer': 'अधिकारी', 'ui.admin': 'प्रशासक', 'ui.language': 'भाषा',
    'ui.connected': 'जुड़ा हुआ', 'ui.offline': 'बैकएंड उपलब्ध नहीं',
    'ui.tables': 'तालिकाएँ', 'ui.data': 'डेटा',

    'draft.meta': 'हाइब्रिड पुनर्प्राप्ति · BM25 + MiniLM → RRF → क्रॉस-एनकोडर → विश्वास सीमा',
    'draft.specText': 'विनिर्देश पाठ', 'draft.whatOfficer': 'अधिकारी क्या खरीदना चाहता है',
    'draft.find': 'शासी मानक खोजें', 'draft.governing': 'शासी मानक',
    'draft.certDuty': 'प्रमाणन दायित्व', 'draft.coCited': 'सह-उद्धृत मानक',
    'draft.clause': 'रचित खंड', 'draft.copyClause': 'खंड कॉपी करें',
    'draft.trace': 'पुनर्प्राप्ति विवरण', 'draft.everyCandidate': 'सीमा द्वारा देखे गए सभी उम्मीदवार',
    'draft.noRule': 'इस मानक के लिए कोई नियम दर्ज नहीं है।',

    'audit.meta': 'इनपुट: PDF, खंड पाठ, या IS संख्याएँ',
    'audit.step1': 'चरण 1', 'audit.step2': 'चरण 2',
    'audit.source': 'स्रोत दस्तावेज़', 'audit.citations': 'सत्यापित करने योग्य उद्धरण',
    'audit.drop': 'निविदा PDF या .docx यहाँ छोड़ें, या चुनने के लिए क्लिक करें',
    'audit.dropHint': 'सर्वर पर पढ़ा गया · 25 MB · पहले 60 पृष्ठ · स्कैन पहचाने जाते हैं, चुपचाप पारित नहीं',
    'audit.orPaste': 'या विनिर्देश पाठ चिपकाएँ',
    'audit.extract': 'IS संख्याएँ निकालें', 'audit.loadCorpus': 'संग्रह से लोड करें',
    'audit.run': 'सत्यापन चलाएँ', 'audit.add': 'जोड़ें',
    'audit.queued': 'कतार में',

    'find.dispute_risk': 'विवाद जोखिम', 'find.statutory_omission': 'वैधानिक चूक',
    'find.missing_connected': 'अनुपस्थित मानक', 'find.not_in_register': 'रजिस्टर में नहीं',
    'find.action': 'कार्रवाई.', 'find.accept': 'स्वीकार करें', 'find.override': 'अस्वीकार करें…',
    'find.accepted': 'स्वीकृत', 'find.overridden': 'अस्वीकृत',
    'find.source': 'स्रोत', 'find.seeCiting': 'उद्धृत करने वाली निविदाएँ देखें',

    'verdict.blocking': 'अवरोधक — इस रूप में प्रकाशित न करें',
    'verdict.review': 'समीक्षा आवश्यक',
    'verdict.clean': 'कोई आपत्ति नहीं',
    'verdict.unreadable': 'दस्तावेज़ पढ़ा नहीं जा सका',

    'dec.log': 'लेखा-परीक्षा अभिलेख', 'dec.newest': 'नवीनतम पहले', 'dec.when': 'कब',
    'dec.officer': 'अधिकारी', 'dec.standard': 'मानक', 'dec.finding': 'निष्कर्ष',
    'dec.verdict': 'निर्णय', 'dec.reason': 'कारण', 'dec.mix': 'निर्णयों का वितरण',
    'dec.logged': 'दर्ज निर्णय', 'dec.agreement': 'सहमति दर',

    'ev.meta': 'कौन-सी प्रकाशित निविदाएँ किसी मानक को उद्धृत करती हैं',
    'ev.lookup': 'मानक खोजें', 'ev.show': 'प्रमाण दिखाएँ',
    'ev.everyRow': 'प्रत्येक पंक्ति स्रोत दस्तावेज़ से जुड़ी है',
    'ev.citing': 'उद्धृत करने वाली निविदाएँ', 'ev.tendersCiting': 'उद्धृत करने वाली निविदाएँ',
    'ev.share': 'संग्रह में हिस्सा', 'ev.inRegister': 'रजिस्टर में',

    'msg.backendDown': 'बैकएंड उपलब्ध नहीं है।',
    'msg.nothingYet': 'अभी कुछ सत्यापित नहीं हुआ',
    'msg.queueFirst': 'कुछ उद्धरण कतार में जोड़ें और सत्यापन चलाएँ।',
    'msg.reqCitation': 'पहले कोई उद्धरण या खंड पाठ जोड़ें',
    'msg.reqSpec': 'पहले कुछ विनिर्देश पाठ दर्ज करें',
    'msg.titlesEnglish': 'मानकों के शीर्षक BIS द्वारा प्रकाशित रूप में अंग्रेज़ी में दिखाए गए हैं।',
  },
};

/* Machine-translated locales generated by translate_ui.py, if present. Merged
   under the hand-written blocks above, never over them: a reviewed string always
   beats a generated one. Adding a language is then a data change, not a code
   change. */
if (typeof LOCALES !== 'undefined') {
  for (const [code, dict] of Object.entries(LOCALES)) {
    STRINGS[code] = { ...dict, ...(STRINGS[code] || {}) };
    if (!LANGS.some(l => l.code === code)) {
      LANGS.push({ code, label: code.toUpperCase(), native: (LOCALE_NAMES || {})[code] || code });
    }
  }
}

let LANG = localStorage.getItem('manak.lang') || 'en';

/* Set the language before any rendering, without firing langchange. boot() calls
   this for ?lang= — writing localStorage there was not enough, because this
   module read localStorage when it was parsed, which happens first. The badge
   said HI while every string still rendered in English. */
function initLang(code) {
  if (code && LANGS.some(l => l.code === code)) {
    STRINGS[code] = STRINGS[code] || {};
    LANG = code;
    localStorage.setItem('manak.lang', code);
  }
  document.documentElement.lang = LANG;
  document.documentElement.dir = RTL.has(LANG) ? 'rtl' : 'ltr';
  return LANG;
}

/* Falls back to English, then to the key itself. A missing translation should
   degrade to a readable word, never to an empty element. */
function t(key) {
  return (STRINGS[LANG] && STRINGS[LANG][key]) || STRINGS.en[key] || key;
}

/* Right-to-left scripts need the document direction set, or Urdu renders
   left-aligned and reads wrongly. */
const RTL = new Set(['ur', 'ks', 'sd']);

function setLang(code) {
  // Accept any language offered in the switcher, not only the two with a
  // hand-written block. Requiring a STRINGS entry meant ten of the twelve
  // languages silently did nothing when selected: keys fall back to English and
  // the page translator renders the rest, which is the whole design.
  if (!LANGS.some(l => l.code === code)) return;
  STRINGS[code] = STRINGS[code] || {};
  LANG = code;
  localStorage.setItem('manak.lang', code);
  document.documentElement.lang = code;
  document.documentElement.dir = RTL.has(code) ? 'rtl' : 'ltr';
  applyI18n();
  document.dispatchEvent(new CustomEvent('langchange', { detail: { lang: code } }));
}

/* Walks data-i18n attributes. Elements carrying data-i18n-attr set that
   attribute instead of their text, for placeholders and titles. */
function applyI18n(root = document) {
  root.querySelectorAll('[data-i18n]:not([data-i18n-skip])').forEach(el => {
    const key = el.dataset.i18n;
    const attr = el.dataset.i18nAttr;
    if (attr) el.setAttribute(attr, t(key));
    else el.textContent = t(key);
  });
}

const langLabel = () => (LANGS.find(l => l.code === LANG) || LANGS[0]).native;

/* ── whole-page translation ─────────────────────────────────────────────────
   The curated dictionary covers the chrome, but most of what an officer reads
   is built at runtime — finding text, table headers, card titles — and stayed
   English when the language changed. This walks the rendered page and
   translates what is left.

   What it never sends, and never alters:

     .mono          IS numbers, scheme codes, tender ids
     .std-title     the register's own titles for standards
     [data-notranslate]
     #fw-clause     the composed clause, which is the text an officer pastes
                    into a tender and must stay in the register's language

   Translations are cached server-side, so the first viewer in a language pays
   for the calls and nobody after them does. Anything that fails stays English
   rather than blanking. */

const NO_TRANSLATE = 'script,style,code,pre,.mono,.std-title,[data-notranslate],#fw-clause,#lang-pop';
const TRANSLATED = new WeakMap();

function collectTextNodes(root = document.body) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      const p = n.parentElement;
      if (!p || p.closest(NO_TRANSLATE)) return NodeFilter.FILTER_REJECT;
      const s = n.nodeValue.trim();
      // Skip pure punctuation, numbers, and anything already in an Indic script.
      if (s.length < 2 || /^[\d\s.,:;·—–\-/()%|]+$/.test(s)) return NodeFilter.FILTER_REJECT;
      if (/[ऀ-෿؀-ۿ]/.test(s)) return NodeFilter.FILTER_REJECT;
      if (!/[A-Za-z]{2}/.test(s)) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const out = [];
  let n;
  while ((n = walker.nextNode())) out.push(n);
  return out;
}

let translating = false;

async function translatePage() {
  const lang = document.documentElement.lang;
  if (!lang || lang === 'en' || translating) return;
  translating = true;
  try {
    const nodes = collectTextNodes();
    const pending = nodes.filter(n => TRANSLATED.get(n) !== lang);
    const unique = [...new Set(pending.map(n => n.nodeValue.trim()))].slice(0, 400);
    if (!unique.length) return;

    const r = await fetch(`${API}/translate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ texts: unique, target: lang }),
      cache: 'no-store',
    });
    if (!r.ok) return;
    const { translations } = await r.json();

    pending.forEach(n => {
      const key = n.nodeValue.trim();
      const hit = translations[key];
      if (!hit) return;
      // Preserve the original leading/trailing whitespace so layout is unchanged.
      n.nodeValue = n.nodeValue.replace(key, hit);
      TRANSLATED.set(n, lang);
    });
  } catch (_) {
    /* leave the page in English; a translation outage must not break it */
  } finally {
    translating = false;
  }
}
