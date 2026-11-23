const form = document.querySelector('form[action="/runs"]');
const pending = document.querySelector("#pending");
const button = form?.querySelector('button[type="submit"]');
let running = false;
form?.addEventListener("submit", (event) => {
  if (running) {
    event.preventDefault();
    return;
  }
  running = true;
  button.setAttribute("aria-disabled", "true");
  pending.textContent = "Nalar sedang menjalankan tugas. Tunggu halaman hasil.";
});
window.addEventListener("pageshow", () => {
  if (!form) return;
  running = false;
  button.removeAttribute("aria-disabled");
  pending.textContent = "";
});
