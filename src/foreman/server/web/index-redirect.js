(function () {
  "use strict";

  const params = new URLSearchParams(location.search);
  params.set("control", "1");
  const query = params.toString();
  location.replace("/app.html" + (query ? "?" + query : "?control=1") + location.hash);
})();
