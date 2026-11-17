// OnRoute · Invitación a la encuesta de satisfacción (Microsoft Forms)
// Aparece abajo, sin tapar la página, unos segundos después de abrirla.
// "Responder" abre la encuesta en otra pestaña y no vuelve a salir en este navegador.
// "Ahora no" la esconde por un día.
const SURVEY_URL = "https://forms.cloud.microsoft/Pages/ResponsePage.aspx?id=ebawul-96E-1Fsa4sxfHgrqs7XJntkNJq-1I-m7foctUN0ZLTlBNWTc0NUpHWk1FTjFINzdNNjBQRi4u";
const SURVEY_DELAY_MS = 20000;            // espera 20 s para no interrumpir apenas entra
const SURVEY_SNOOZE_MS = 24 * 3600 * 1000; // "Ahora no" = vuelve a preguntar mañana

(function () {
  const box = $("survey");
  if (!box) return;
  let state = null;
  try { state = JSON.parse(localStorage.getItem("onrouteSurvey") || "null"); } catch (e) {}
  if (state && (state.done || Date.now() - state.later < SURVEY_SNOOZE_MS)) return;

  const save = (v) => { try { localStorage.setItem("onrouteSurvey", JSON.stringify(v)); } catch (e) {} };
  const hide = () => { box.classList.remove("show"); setTimeout(() => box.classList.add("d-none"), 300); };

  $("svOpen").href = SURVEY_URL;
  $("svOpen").addEventListener("click", () => { save({ done: true }); hide(); });
  $("svLater").addEventListener("click", () => { save({ later: Date.now() }); hide(); });
  $("svClose").addEventListener("click", () => { save({ later: Date.now() }); hide(); });

  function show() {
    // si la bienvenida de la primera visita sigue abierta, se espera a que la cierren
    const wl = $("welcome");
    if (wl && !wl.classList.contains("d-none")) { setTimeout(show, 5000); return; }
    box.classList.remove("d-none");
    requestAnimationFrame(() => box.classList.add("show"));
  }
  setTimeout(show, SURVEY_DELAY_MS);
})();
