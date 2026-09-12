/* Open the engine tab when following a link to one of its sections. */
(function () {
  function openLinkedTab() {
    var target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    var block = target && target.closest(".tabbed-block");
    if (!block) return;

    var tabs = block.closest(".tabbed-set");
    var index = Array.from(block.parentElement.children).indexOf(block);
    var input = tabs.querySelectorAll(":scope > input")[index];
    if (!input.checked) input.click();
    target.scrollIntoView();
  }

  document$.subscribe(openLinkedTab);
  window.addEventListener("hashchange", openLinkedTab);
})();
