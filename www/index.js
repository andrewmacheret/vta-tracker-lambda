// Add ?api=http://localhost:8000 to the page URL to try a different server
const apiUrl = new URLSearchParams(location.search).get('api') || 'https://vta-tracker.andrewmacheret.com';
const refreshMs = 15000;

const map = L.map('map').setView([37.33, -121.89], 11);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}).addTo(map);

const statusText = document.getElementById('status-text');
// trip_id -> Leaflet marker
const markers = new Map();

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function vehicleIcon(v) {
  const span = document.createElement('span');
  span.textContent = v.label;
  span.style.background = v.color;
  span.style.color = v.text_color;
  let arrow = '';
  if (v.bearing !== null && v.bearing !== undefined) {
    // Only the arrow turns, so the label stays readable.
    // Push it out to just past the label's edge, which is further for wide labels and sideways arrows.
    const halfWidth = v.label.length * (v.rail ? 4 : 3.5) + 10;
    const halfHeight = 13;
    const angle = v.bearing * Math.PI / 180;
    const distance = Math.round(1 / Math.hypot(Math.sin(angle) / halfWidth, Math.cos(angle) / halfHeight)) + 8;
    arrow = `<i class="arrow" style="--distance: ${distance}px; transform: rotate(${v.bearing}deg); ` +
      `border-bottom-color: ${v.color}"></i>`;
  }
  // iconSize null lets the label size itself
  return L.divIcon({ className: v.rail ? 'vehicle rail' : 'vehicle', html: arrow + span.outerHTML, iconSize: null });
}

function tooltip(v) {
  return `<strong>${escapeHtml(v.label)}</strong> ${escapeHtml(v.name)}<br>To ${escapeHtml(v.headsign)}`;
}

function showVehicles(data) {
  const seen = new Set();
  for (const v of data.vehicles) {
    seen.add(v.trip_id);
    const marker = markers.get(v.trip_id);
    if (marker) {
      marker.setLatLng([v.lat, v.lon]);
      if (marker.bearing !== v.bearing) {
        marker.setIcon(vehicleIcon(v));
        marker.bearing = v.bearing;
      }
    } else {
      const created = L.marker([v.lat, v.lon], { icon: vehicleIcon(v), zIndexOffset: v.rail ? 1000 : 0 })
        .bindTooltip(tooltip(v))
        .addTo(map);
      created.bearing = v.bearing;
      markers.set(v.trip_id, created);
    }
  }
  for (const [tripId, marker] of markers) {
    if (!seen.has(tripId)) {
      marker.remove();
      markers.delete(tripId);
    }
  }
}

function feedEnded(feed, time) {
  // feed.end_date looks like 20270110, time looks like 2026-11-19T22:20:52-08:00
  return feed.end_date && time.slice(0, 10).replaceAll('-', '') > feed.end_date;
}

function showStatus(data) {
  const time = new Date(data.time).toLocaleTimeString();
  const count = data.vehicles.length;
  if (feedEnded(data.feed, data.time)) {
    statusText.className = 'error';
    statusText.textContent = `The timetable ran out on ${data.feed.end_date}. Waiting for VTA to publish a new one.`;
  } else {
    statusText.className = '';
    statusText.textContent = count === 0
      ? `Nothing scheduled to be running at ${time}.`
      : `${count} ${count === 1 ? 'vehicle' : 'vehicles'} at ${time}.`;
  }
}

async function refresh() {
  try {
    const response = await fetch(`${apiUrl}/vehicles`, { signal: AbortSignal.timeout(10000) });
    const data = await response.json();
    if (!response.ok) {
      throw new Error((data.errors || [response.statusText]).join(', '));
    }
    showVehicles(data);
    showStatus(data);
  } catch (error) {
    statusText.className = 'error';
    statusText.textContent = `Could not load vehicles: ${error.message}`;
  }
}

// Don't poll while the tab is in the background
let timer = null;
function start() {
  if (timer === null) {
    refresh();
    timer = setInterval(refresh, refreshMs);
  }
}
function stop() {
  clearInterval(timer);
  timer = null;
}
document.addEventListener('visibilitychange', () => (document.hidden ? stop() : start()));
start();
