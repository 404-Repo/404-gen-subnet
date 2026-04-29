export default function generate(THREE) {
  const group = new THREE.Group();

  const VW = 0.62;
  const VL = 0.92;

  const wheelR = 0.042;
  const wheelBot = -0.23;
  const wheelCY = wheelBot + wheelR;
  const bodyBot = -0.17;
  const belt = 0.005;
  const roofBot = 0.16;
  const roofTop = 0.185;
  const rackY = 0.20;

  const tireThick = wheelR * 0.35;
  const torusR = wheelR - tireThick;

  const bodyMat = new THREE.MeshStandardMaterial({ color: 0xC8B896, roughness: 0.6, metalness: 0.1 });
  const blackMat = new THREE.MeshStandardMaterial({ color: 0x222222, roughness: 0.7, metalness: 0.05 });
  const darkMat = new THREE.MeshStandardMaterial({ color: 0x1A1A1A, roughness: 0.8, metalness: 0.05 });
  const chromeMat = new THREE.MeshStandardMaterial({ color: 0xC0C0C0, roughness: 0.1, metalness: 0.9 });
  const glassMat = new THREE.MeshPhysicalMaterial({
    color: 0x8899AA, roughness: 0.1, metalness: 0.0,
    transmission: 0.5, transparent: true, opacity: 0.6
  });
  const lensMat = new THREE.MeshStandardMaterial({
    color: 0xFFFFDD, roughness: 0.3, metalness: 0.2,
    emissive: 0xFFFFDD, emissiveIntensity: 0.15
  });
  const rackMat = new THREE.MeshStandardMaterial({ color: 0x333333, roughness: 0.5, metalness: 0.3 });
  const tireMat = new THREE.MeshStandardMaterial({ color: 0x1A1A1A, roughness: 0.9, metalness: 0.0 });
  const hubMat = new THREE.MeshStandardMaterial({ color: 0x3A3A3A, roughness: 0.4, metalness: 0.6 });
  const indicatorMat = new THREE.MeshStandardMaterial({ color: 0xFFAA33, roughness: 0.4, metalness: 0.1 });
  const tailMat = new THREE.MeshStandardMaterial({
    color: 0xCC2222, roughness: 0.3, metalness: 0.1,
    emissive: 0xCC2222, emissiveIntensity: 0.1
  });

  function addBox(w, h, d, mat, x, y, z) {
    var m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
    m.position.set(x, y, z);
    group.add(m);
    return m;
  }

  function addTube(p1, p2, r, mat) {
    var m = new THREE.Mesh(
      new THREE.TubeGeometry(new THREE.LineCurve3(p1, p2), 1, r, 6, false),
      mat
    );
    group.add(m);
    return m;
  }

  // Lower body
  var lbW = VW * 0.80;
  var lbH = belt - bodyBot;
  var lbL = VL * 0.88;
  addBox(lbW, lbH, lbL, bodyMat, 0, bodyBot + lbH / 2, 0);

  // Upper cabin
  var cabW = VW * 0.74;
  var cabH = roofBot - belt;
  var cabL = VL * 0.58;
  var cabZ = -VL * 0.08;
  addBox(cabW, cabH, cabL, bodyMat, 0, belt + cabH / 2, cabZ);

  // Hood
  var hoodW = lbW * 0.92;
  var hoodH = 0.012;
  var hoodL = VL * 0.20;
  var hoodZ = cabZ + cabL / 2 + hoodL / 2 + 0.005;
  addBox(hoodW, hoodH, hoodL, bodyMat, 0, belt, hoodZ);

  // Roof
  var roofW = VW * 0.70;
  var roofH = roofTop - roofBot;
  var roofL = cabL * 0.96;
  addBox(roofW, roofH, roofL, blackMat, 0, roofBot + roofH / 2, cabZ);

  // Roof rack frame
  var rkW = VW * 0.55;
  var rkL = VL * 0.42;
  var rkZ = cabZ;
  var tubR = 0.004;

  var corners = [
    new THREE.Vector3(-rkW / 2, rackY, rkZ - rkL / 2),
    new THREE.Vector3(rkW / 2, rackY, rkZ - rkL / 2),
    new THREE.Vector3(rkW / 2, rackY, rkZ + rkL / 2),
    new THREE.Vector3(-rkW / 2, rackY, rkZ + rkL / 2)
  ];

  for (var i = 0; i < 4; i++) {
    addTube(corners[i], corners[(i + 1) % 4], tubR, rackMat);
  }

  for (var ci = 1; ci <= 3; ci++) {
    var cz = corners[0].z + (corners[3].z - corners[0].z) * (ci / 4);
    addTube(
      new THREE.Vector3(-rkW / 2, rackY, cz),
      new THREE.Vector3(rkW / 2, rackY, cz),
      tubR, rackMat
    );
  }

  for (var li = 0; li < corners.length; li++) {
    var c = corners[li];
    addTube(c, new THREE.Vector3(c.x, roofTop + 0.002, c.z), tubR, rackMat);
  }

  // Windshield
  var wsW = VW * 0.64;
  var wsH = cabH * 0.78;
  var wsFrontZ = cabZ + cabL / 2 + 0.003;
  addBox(wsW, wsH, 0.005, glassMat, 0, belt + cabH * 0.12 + wsH / 2, wsFrontZ);

  // Rear window
  var rwW = wsW * 0.82;
  var rwH = wsH * 0.72;
  addBox(rwW, rwH, 0.005, glassMat, 0, belt + cabH * 0.16 + rwH / 2, cabZ - cabL / 2 - 0.003);

  // Side windows
  var swH = cabH * 0.55;
  var swY = belt + cabH * 0.22 + swH / 2;

  for (var side = -1; side <= 1; side += 2) {
    var sx = side * (cabW / 2 + 0.003);
    var swFrontL = cabL * 0.30;
    var swRearL = cabL * 0.25;
    var swGap = cabL * 0.05;
    var swBase = cabZ + cabL / 2 - cabL * 0.06;
    addBox(0.005, swH, swFrontL, glassMat, sx, swY, swBase - swFrontL / 2);
    addBox(0.005, swH * 0.92, swRearL, glassMat, sx, swY, swBase - swFrontL - swGap - swRearL / 2);
  }

  // Front grille
  var grW = VW * 0.34;
  var grH = 0.055;
  var grZ = lbL / 2 + 0.005;
  var grY = bodyBot + lbH * 0.52;
  addBox(grW, grH, 0.012, darkMat, 0, grY, grZ);

  for (var si = 0; si < 5; si++) {
    var st = (si + 0.5) / 5;
    addBox(grW * 0.84, grH * 0.08, 0.016, chromeMat, 0, grY - grH / 2 + grH * st, grZ + 0.004);
  }

  // Headlights
  var hlR = VW * 0.038;

  for (var hs = -1; hs <= 1; hs += 2) {
    var hx = hs * (grW / 2 + hlR + 0.018);
    var hy = grY + 0.005;
    var hz = grZ;

    var rim = new THREE.Mesh(
      new THREE.CylinderGeometry(hlR + 0.005, hlR + 0.005, 0.008, 16),
      chromeMat
    );
    rim.rotation.x = Math.PI / 2;
    rim.position.set(hx, hy, hz);
    group.add(rim);

    var lens = new THREE.Mesh(
      new THREE.CylinderGeometry(hlR, hlR, 0.012, 16),
      lensMat
    );
    lens.rotation.x = Math.PI / 2;
    lens.position.set(hx, hy, hz + 0.002);
    group.add(lens);

    addBox(hlR * 1.5, hlR * 0.6, 0.008, indicatorMat, hx, grY - grH * 0.38, grZ + 0.005);
  }

  // Front bumper
  var bmpW = VW * 0.84;
  var bmpH = 0.025;
  var bmpD = 0.032;
  addBox(bmpW, bmpH, bmpD, blackMat, 0, bodyBot + bmpH / 2, lbL / 2 + bmpD / 2);

  // Rear bumper
  addBox(bmpW, bmpH, bmpD, blackMat, 0, bodyBot + bmpH / 2, -lbL / 2 - bmpD / 2);

  // Fenders
  for (var fs = -1; fs <= 1; fs += 2) {
    addBox(0.012, lbH * 0.22, lbL * 0.9, blackMat, fs * (lbW / 2 + 0.005), bodyBot + lbH * 0.11, 0);
  }

  // Wheels and arches
  var wInX = lbW / 2;
  var wFZ = VL * 0.30;
  var wRZ = -VL * 0.30;

  var wheelPositions = [
    [-wInX, wheelCY, wFZ],
    [wInX, wheelCY, wFZ],
    [-wInX, wheelCY, wRZ],
    [wInX, wheelCY, wRZ]
  ];

  for (var wi = 0; wi < wheelPositions.length; wi++) {
    var wp = wheelPositions[wi];
    var wx = wp[0], wy = wp[1], wz = wp[2];

    var tire = new THREE.Mesh(
      new THREE.TorusGeometry(torusR, tireThick, 10, 24),
      tireMat
    );
    tire.rotation.z = Math.PI / 2;
    tire.position.set(wx, wy, wz);
    group.add(tire);

    var hub = new THREE.Mesh(
      new THREE.CylinderGeometry(wheelR * 0.42, wheelR * 0.42, 0.015, 12),
      hubMat
    );
    hub.rotation.z = Math.PI / 2;
    hub.position.set(wx, wy, wz);
    group.add(hub);

    var cap = new THREE.Mesh(
      new THREE.CylinderGeometry(wheelR * 0.12, wheelR * 0.12, 0.018, 8),
      chromeMat
    );
    cap.rotation.z = Math.PI / 2;
    cap.position.set(wx, wy, wz);
    group.add(cap);

    var sideDir = wx > 0 ? 1 : -1;
    addBox(
      0.016, wheelR * 2.3, wheelR * 2.5,
      blackMat,
      wx + sideDir * 0.014, wy + wheelR * 0.35, wz
    );
  }

  // Tail lights
  var tlW = 0.022;
  var tlH = 0.032;
  for (var ts = -1; ts <= 1; ts += 2) {
    addBox(tlW, tlH, 0.008, tailMat,
      ts * (lbW / 2 - tlW * 0.6), bodyBot + lbH * 0.55, -lbL / 2 - 0.003);
  }

  // Spare tire on rear door
  var spareThick = tireThick * 0.8;
  var spareTorusR = wheelR * 0.95 - spareThick;
  var spareZ = -lbL / 2 - bmpD - spareThick - 0.008;
  var spareY = bodyBot + lbH * 0.5;

  var spareTire = new THREE.Mesh(
    new THREE.TorusGeometry(spareTorusR, spareThick, 8, 20),
    tireMat
  );
  spareTire.rotation.x = Math.PI / 2;
  spareTire.position.set(0, spareY, spareZ);
  group.add(spareTire);

  var spareHub = new THREE.Mesh(
    new THREE.CylinderGeometry(wheelR * 0.35, wheelR * 0.35, 0.012, 10),
    hubMat
  );
  spareHub.rotation.x = Math.PI / 2;
  spareHub.position.set(0, spareY, spareZ);
  group.add(spareHub);

  // Side steps
  for (var ss = -1; ss <= 1; ss += 2) {
    addBox(0.028, 0.007, VL * 0.42, blackMat, ss * (lbW / 2 + 0.012), bodyBot + 0.008, 0);
  }

  // Side mirrors
  for (var ms = -1; ms <= 1; ms += 2) {
    var mirrorX = ms * (cabW / 2 + 0.018);
    var mirrorY = belt + cabH * 0.55;
    var mirrorZ = cabZ + cabL / 2 - cabL * 0.02;
    addBox(0.005, 0.018, 0.022, blackMat, mirrorX, mirrorY, mirrorZ);
    addBox(0.003, 0.014, 0.018, glassMat, mirrorX + ms * 0.003, mirrorY, mirrorZ);
  }

  // Door seam lines
  for (var ds = -1; ds <= 1; ds += 2) {
    var doorX = ds * (cabW / 2 + 0.001);
    addBox(0.002, cabH * 0.92, 0.003, darkMat, doorX, belt + cabH * 0.5, cabZ + cabL * 0.08);
  }

  // Front tow hooks
  for (var th = -1; th <= 1; th += 2) {
    addBox(0.015, 0.012, 0.02, chromeMat,
      th * bmpW * 0.38, bodyBot + bmpH * 0.5, lbL / 2 + bmpD + 0.005);
  }

  return group;
}
