(() => {
  const openButton = document.getElementById("dostiris-chat-button");
  const closeButton = document.getElementById("dostiris-chat-close");
  const widget = document.getElementById("dostiris-chat-widget");
  if (!openButton || !closeButton || !widget) return;

  function openMia() {
    widget.style.display = "block";
    closeButton.style.display = "block";
    openButton.style.display = "none";
  }

  function closeMia() {
    widget.style.display = "none";
    closeButton.style.display = "none";
    openButton.style.display = "block";
  }

  openButton.addEventListener("click", openMia);
  closeButton.addEventListener("click", closeMia);
})();
