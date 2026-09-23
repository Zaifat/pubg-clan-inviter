// Interface texts come from ui/lang/<code>.json through the Python side.
let STR = {};

function T(key, vars) {
  let s = STR[key] || key;
  if (vars) for (const k in vars) s = s.replaceAll("{" + k + "}", vars[k]);
  return s;
}

// data-i18n: text, data-i18n-html: markup (hints), data-i18n-title / data-i18n-ph: attributes
function applyStatic() {
  document.querySelectorAll("[data-i18n]").forEach(el => el.textContent = T(el.dataset.i18n));
  document.querySelectorAll("[data-i18n-html]").forEach(el => el.innerHTML = T(el.dataset.i18nHtml));
  document.querySelectorAll("[data-i18n-title]").forEach(el => el.title = T(el.dataset.i18nTitle));
  document.querySelectorAll("[data-i18n-ph]").forEach(el => el.placeholder = T(el.dataset.i18nPh));
  document.documentElement.style.setProperty("--log-empty", JSON.stringify(T("log_empty")));
}

async function loadLang() {
  const r = await window.pywebview.api.get_strings();
  STR = r.strings;
  document.documentElement.lang = r.current;
  applyStatic();
  return r;
}
