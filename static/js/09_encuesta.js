// OnRoute · Invitación a la encuesta de satisfacción (Microsoft Forms)
// Aparece abajo, sin tapar la página, apenas carga (la gente a veces no dura ni 20 s).
// "Responder" abre la encuesta en otra pestaña y no vuelve a salir en este navegador.
// "Ahora no" (o la X) la esconde y vuelve a preguntar a los 30 s, máximo 2 veces más por visita.
const SURVEY_URL = "https://forms.cloud.microsoft/Pages/ResponsePage.aspx?id=ebawul-96E-1Fsa4sxfHgrqs7XJntkNJq-1I-m7foctUN0ZLTlBNWTc0NUpHWk1FTjFINzdNNjBQRi4u";
const SURVEY_DELAY_MS = 1500;    // casi enseguida, cuando ya cargó la página
const SURVEY_RETRY_MS = 30000;   // después de "Ahora no", vuelve a preguntar a los 30 s
const SURVEY_MAX_RETRIES = 2;    // y lo hace máximo 2 veces más en la misma visita

(function () {
  const box = $("survey");
  if (!box) return;
  let done = false;
  try { done = localStorage.getItem("onrouteSurveyDone") === "1"; } catch (e) {}
  if (done) return;

  let retries = 0;
  const hide = () => { box.classList.remove("show"); setTimeout(() => box.classList.add("d-none"), 300); };
  const show = () => { box.classList.remove("d-none"); requestAnimationFrame(() => box.classList.add("show")); };
  const later = () => {
    hide();
    if (retries < SURVEY_MAX_RETRIES) { retries++; setTimeout(show, SURVEY_RETRY_MS); }
  };

  $("svOpen").href = SURVEY_URL;
  $("svOpen").addEventListener("click", () => {
    try { localStorage.setItem("onrouteSurveyDone", "1"); } catch (e) {}
    hide();
  });
  $("svLater").addEventListener("click", later);
  $("svClose").addEventListener("click", later);

  setTimeout(show, SURVEY_DELAY_MS);
})();
