// RTD owns the version/language flyout. Keep local MkDocs search until its
// hosted Addons announce that they are ready.
document.addEventListener("readthedocs-addons-data-ready", function () {
  var input = document.querySelector(".md-search__input");
  if (!input || input.dataset.rtdSearch) return;
  input.dataset.rtdSearch = "true";
  input.addEventListener("focus", function () {
    document.dispatchEvent(new CustomEvent("readthedocs-search-show"));
  });
});
