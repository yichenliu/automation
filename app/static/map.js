// Maps: each element with data-pins becomes a map of the records with a location; clicking a pin opens the record.
(function () {
  "use strict";
  if (!window.L) return;
  const colours = {red: "#dc2626", orange: "#ea580c", amber: "#d97706", yellow: "#ca8a04", green: "#16a34a", teal: "#0d9488",
                   blue: "#2563eb", indigo: "#4f46e5", violet: "#7c3aed", pink: "#db2777", gray: "#6b7280"};
  const palette = Object.values(colours);
  document.querySelectorAll("[data-pins]").forEach((box) => {
    const pins = JSON.parse(box.dataset.pins || "[]");
    if (!pins.length) return;
    const map = window.L.map(box, {scrollWheelZoom: !box.classList.contains("small-map")});
    window.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19, attribution: "© OpenStreetMap contributors"}).addTo(map);
    const seen = {};
    const colourOf = (name) => colours[name] || (name ? (seen[name] ??= palette[Object.keys(seen).length % palette.length]) : "#5b5bd6");
    const bounds = [];
    pins.forEach((p) => {
      const marker = window.L.circleMarker([p.lat, p.lng], {radius: 8, weight: 2, color: "#fff", fillColor: colourOf(p.colour), fillOpacity: 0.95});
      const link = document.createElement("a");
      link.href = p.url; link.textContent = p.title;
      marker.bindPopup(link).bindTooltip(p.title + (p.colour ? ` · ${p.colour}` : "")).addTo(map);
      bounds.push([p.lat, p.lng]);
    });
    if (bounds.length === 1) map.setView(bounds[0], 14); else map.fitBounds(bounds, {padding: [30, 30], maxZoom: 15});
  });
})();
