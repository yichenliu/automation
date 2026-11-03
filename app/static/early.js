// Runs before the page draws: mark pages shown inside the builder's preview, so they hide what only makes sense standalone.
if (window.self !== window.top) document.documentElement.classList.add("embedded");
