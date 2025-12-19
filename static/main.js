console.log("main.js LOADED (Japan AQI)");

let stationPoints = [];
let stationMarkers = [];

const map = L.map("map").setView([36, 138], 5);

L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  {
    maxZoom: 18,
  }
).addTo(map);

let allPoints = [];
let gridVals = null;
let heatLayers = [];
let hoverbox = document.getElementById("hoverinfo");
let crosshair = document.getElementById("crosshair");

function getAQIColor(aqi){
  if (aqi <= 50)  return "#00e400";
  if (aqi <= 100) return "#ffff00";
  if (aqi <= 150) return "#ff7e00";
  if (aqi <= 200) return "#ff0000";
  if (aqi <= 300) return "#8f3f97";
  return "#7e0023";
}

function refreshData(){
  setStatus("Fetching WAQI points...");
  fetch("/api/air")
    .then(r => r.json())
    .then(data => {
      allPoints = data || [];
      stationPoints = allPoints.slice();

      console.log("Received points:", allPoints.length);

      renderStationMarkers();
      applyGrid();
      populateCityTable();

      setStatus("Fetched " + allPoints.length + " stations");
    })
    .catch(err => {
      console.error(err);
      setStatus("Fetch error");
    });
}

function renderStationMarkers(){
  stationMarkers.forEach(m => map.removeLayer(m));
  stationMarkers = [];

  stationPoints.forEach(p => {
    const marker = L.circleMarker([p.lat, p.lon], {
      radius: 6,
      color: "#000",
      weight: 1,
      fillColor: getAQIColor(p.aqi),
      fillOpacity: 1
    }).addTo(map);

    marker.bindPopup(`
      <b>${p.name}</b><br>
      AQI: ${p.aqi}<br>
      Lat: ${p.lat.toFixed(3)}, Lon: ${p.lon.toFixed(3)}
    `);

    stationMarkers.push(marker);
  });
}

function sqDist(aLat,aLon,bLat,bLon){
  const dy = aLat - bLat;
  const dx = aLon - bLon;
  return dy*dy + dx*dx;
}

function applyGrid(){
  if (!allPoints.length){
    setStatus("No points to grid");
    return;
  }

  const rows = parseInt(document.getElementById("inpRows").value) || 100;
  const cols = parseInt(document.getElementById("inpCols").value) || 160;
  const k    = parseInt(document.getElementById("inpK").value)    || 6;

  let minLat = Infinity, maxLat = -Infinity;
  let minLon = Infinity, maxLon = -Infinity;

  allPoints.forEach(p => {
      if (p.lat < minLat) minLat = p.lat;
      if (p.lat > maxLat) maxLat = p.lat;
      if (p.lon < minLon) minLon = p.lon;
      if (p.lon > maxLon) maxLon = p.lon;
  });

  minLat -= 0.4;
  maxLat += 0.4;
  minLon -= 0.4;
  maxLon += 0.4;

  const dlat = (maxLat - minLat) / (rows - 1);
  const dlon = (maxLon - minLon) / (cols - 1);

  const vals = Array.from({ length: rows }, () => new Array(cols).fill(0));

  const pts = allPoints.map(p => ({
    lat: p.lat,
    lon: p.lon,
    aqi: p.aqi
  }));

  for (let i = 0; i < rows; i++){
    const lat = minLat + i * dlat;
    for (let j = 0; j < cols; j++){
      const lon = minLon + j * dlon;
      vals[i][j] = idwEstimate(lat, lon, pts, k, 2.0);
    }
  }

  gridVals = { rows, cols, minLat, minLon, dlat, dlon, vals };
  renderGridHeat(gridVals);
  setStatus("Grid ready (" + rows + "x" + cols + ")");
}

function idwEstimate(lat,lon,pts,k=6,power=2){
  if (!pts.length) return 0;

  const arr = [];
  for (let i = 0; i < pts.length; i++){
    const p = pts[i];
    const d2 = sqDist(lat, lon, p.lat, p.lon) + 1e-12;
    arr.push({ d2, aqi: p.aqi });
  }

  arr.sort((a,b) => a.d2 - b.d2);

  let sumW = 0;
  let sumWA = 0;
  const n = Math.min(k, arr.length);

  for (let t = 0; t < n; t++){
    const w = 1 / Math.pow(Math.sqrt(arr[t].d2), power);
    sumW += w;
    sumWA += w * arr[t].aqi;
  }

  return sumW === 0 ? 0 : sumWA / sumW;
}

function renderGridHeat(g){
  heatLayers.forEach(l => map.removeLayer(l));
  heatLayers = [];

  const groups = {
    good: [], moderate: [], sensitive: [],
    unhealthy: [], very: [], hazardous: []
  };

  const colors = {
    good: "#00e400",
    moderate: "#ffff00",
    sensitive: "#ff7e00",
    unhealthy: "#ff0000",
    very: "#8f3f97",
    hazardous: "#7e0023"
  };

  for (let i = 0; i < g.rows; i++){
    const lat = g.minLat + i * g.dlat;
    for (let j = 0; j < g.cols; j++){
      const lon = g.minLon + j * g.dlon;
      const aqi = g.vals[i][j];

      const bucket =
        aqi <= 50   ? "good" :
        aqi <= 100  ? "moderate" :
        aqi <= 150  ? "sensitive" :
        aqi <= 200  ? "unhealthy" :
        aqi <= 300  ? "very" : "hazardous";

      const weight = Math.min(Math.max(aqi / 150, 0), 1);
      groups[bucket].push([lat, lon, weight]);
    }
  }

  for (const c in groups){
    if (!groups[c].length) continue;

    const layer = L.heatLayer(groups[c], {
      radius: 25,
      blur: 10,
      maxOpacity: 0.5,
      minOpacity: 0.3,
      gradient: { 1.0: colors[c] }
    }).addTo(map);

    heatLayers.push(layer);
  }

  setTimeout(() => {
    heatLayers.forEach(layer => {
      const canvas = layer._heat?._canvas;
      if (canvas) canvas.classList.add("heatmap-canvas");
    });
  }, 80);

  updateFog();
}

function getGridValueAt(lat, lon){
  if (!gridVals) return null;

  const g = gridVals;
  const i = Math.round((lat - g.minLat) / g.dlat);
  const j = Math.round((lon - g.minLon) / g.dlon);

  if (i < 0 || i >= g.rows || j < 0 || j >= g.cols) return null;
  return g.vals[i][j];
}

function findStationAt(lat, lon){
  if (!stationPoints.length) return null;

  let best = null;
  let bestD = 1e9;

  stationPoints.forEach(s => {
    const d = sqDist(lat, lon, s.lat, s.lon);
    if (d < bestD){
      bestD = d;
      best = s;
    }
  });

  const meters = Math.sqrt(bestD) * 111000;
  return meters < 800 ? best : null;
}

map.on("mousemove", e => {
  const p = map.latLngToContainerPoint(e.latlng);

  crosshair.style.display = "block";
  crosshair.querySelector(".hline").style.top  = p.y + "px";
  crosshair.querySelector(".vline").style.left = p.x + "px";

  const st = findStationAt(e.latlng.lat, e.latlng.lng);

  if (st){
    hoverbox.style.display = "block";
    hoverbox.style.left = (p.x+18) + "px";
    hoverbox.style.top  = (p.y+18) + "px";
    hoverbox.innerHTML = `
      <div style="color:${getAQIColor(st.aqi)}; font-size:18px; font-weight:900;">
        AQI: ${st.aqi}
      </div>
      <div style="font-weight:700;">
        ${st.name}
      </div>
      <div style="font-size:11px;">
        Lat: ${st.lat.toFixed(3)}, Lon: ${st.lon.toFixed(3)}
      </div>
    `;
    return;
  }

  const v = getGridValueAt(e.latlng.lat, e.latlng.lng);
  if (v == null){
    hoverbox.style.display = "none";
    return;
  }

  const aqi = Math.round(v);
  hoverbox.style.display = "block";
  hoverbox.style.left = (p.x+18) + "px";
  hoverbox.style.top  = (p.y+18) + "px";
  hoverbox.innerHTML = `
    <div style="color:${getAQIColor(aqi)}; font-size:18px; font-weight:900;">
      AQI: ${aqi}
    </div>
    <div style="font-size:11px;">
      Lat: ${e.latlng.lat.toFixed(3)}, Lon: ${e.latlng.lng.toFixed(3)}
    </div>
  `;
});

map.on("mouseout", () => {
  hoverbox.style.display = "none";
  crosshair.style.display = "none";
});

function updateFog() {
    const zoom = map.getZoom();
    let opacity = 0.5;

    if (zoom >= 11) opacity = 0.0;
    else if (zoom >= 9) opacity = 0.5 * (11 - zoom) / 2;

    document.querySelectorAll(".heatmap-canvas").forEach(c => {
        c.style.opacity = opacity;
    });
}

function setStatus(t){
  const el = document.getElementById("status");
  if (el) el.innerText = "Status: " + t;
}

window.applyGrid = applyGrid;
window.refreshData = refreshData;

function populateCityTable(){
  const tbody = document.getElementById("cityTableBody");
  if (!tbody) return;

  tbody.innerHTML = "";
  const rows = stationPoints.map(st => ({
    name: st.name || "(unknown)",
    lat: st.lat,
    lon: st.lon,
    aqi: st.aqi || 0
  }));

  rows.sort((a, b) => (b.aqi || 0) - (a.aqi || 0));

  rows.forEach(st => {
    const tr = document.createElement("tr");
    tr.onclick = () => flyToStation(st);

    const tdName = document.createElement("td");
    tdName.className = "city-name";
    tdName.textContent = st.name;

    const tdAQI = document.createElement("td");
    tdAQI.className = "city-aqi";
    tdAQI.textContent = Math.round(st.aqi || 0);
    tdAQI.style.backgroundColor = getAQIColor(st.aqi || 0);
    tdAQI.style.color = (st.aqi || 0) > 150 ? "#fff" : "#000";

    tr.appendChild(tdName);
    tr.appendChild(tdAQI);
    tbody.appendChild(tr);
  });
}

function flyToStation(st){
  map.flyTo([st.lat, st.lon], 11, {
    duration: 1.2,
    easeLinearity: 0.25
  });
}

const searchInput = document.getElementById("citySearch");
if (searchInput){
  searchInput.addEventListener("input", function(){
    const query = this.value.toLowerCase().trim();
    const rows = document.querySelectorAll("#cityTableBody tr");

    rows.forEach(row => {
      const cityName = row.querySelector(".city-name").textContent.toLowerCase();
      if (cityName.includes(query)){
        row.style.display = "";
      } else {
        row.style.display = "none";
      }
    });
  });
}

map.on("zoomend", updateFog);

refreshData();
