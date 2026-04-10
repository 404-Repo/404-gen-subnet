export default function generate(THREE) {
  const group = new THREE.Group();
  const PI = Math.PI;

  const goldMat = new THREE.MeshStandardMaterial({ color: 0xC8960A, metalness: 0.8, roughness: 0.25 });
  const faceMat = new THREE.MeshStandardMaterial({ color: 0xF5F0E0, metalness: 0.0, roughness: 0.4 });
  const darkMat = new THREE.MeshStandardMaterial({ color: 0x2A1A0A, metalness: 0.0, roughness: 0.5 });
  const handMat = new THREE.MeshStandardMaterial({ color: 0x111111, metalness: 0.1, roughness: 0.5 });

  const OR = 0.43;
  const D = 0.16;
  const TB = 0.018;
  const FR = 0.34;
  const NR = 0.265;
  const fz = D / 2;

  var caseMesh = new THREE.Mesh(
    new THREE.CylinderGeometry(OR, OR, D, 64),
    goldMat
  );
  caseMesh.rotation.x = PI / 2;
  group.add(caseMesh);

  var bezelGeo = new THREE.TorusGeometry(OR - TB, TB, 16, 64);
  var bezelF = new THREE.Mesh(bezelGeo, goldMat);
  bezelF.position.z = fz;
  group.add(bezelF);

  var bezelB = new THREE.Mesh(bezelGeo, goldMat);
  bezelB.position.z = -fz;
  group.add(bezelB);

  var fMesh = new THREE.Mesh(
    new THREE.CircleGeometry(FR, 64),
    faceMat
  );
  fMesh.position.z = fz + 0.001;
  group.add(fMesh);

  var ringMesh = new THREE.Mesh(
    new THREE.RingGeometry(FR - 0.004, FR, 64),
    goldMat
  );
  ringMesh.position.z = fz + 0.0015;
  group.add(ringMesh);

  var bw = 0.0035, bh = 0.022, bd = 0.002;

  function buildNumeral(h) {
    var ng = new THREE.Group();
    var s = 0.007;

    function addBar(dx, rot, w, ht) {
      var g = new THREE.BoxGeometry(w || bw, ht || bh, bd);
      var m = new THREE.Mesh(g, darkMat);
      m.position.x = dx;
      if (rot) m.rotation.z = rot;
      ng.add(m);
    }

    function I(x) { addBar(x, 0); }

    function V(cx) {
      addBar(cx - 0.004, 0.3);
      addBar(cx + 0.004, -0.3);
    }

    function X(cx) {
      addBar(cx, 0.35);
      addBar(cx, -0.35);
    }

    switch (h) {
      case 1: I(0); break;
      case 2: I(-s / 2); I(s / 2); break;
      case 3: I(-s); I(0); I(s); break;
      case 4: I(-1.5 * s); I(-0.5 * s); I(0.5 * s); I(1.5 * s); break;
      case 5: V(0); break;
      case 6: V(-0.005); I(0.01); break;
      case 7: V(-0.008); I(0.005); I(0.012); break;
      case 8: V(-0.012); I(0.002); I(0.009); I(0.016); break;
      case 9: I(-0.008); X(0.005); break;
      case 10: X(0); break;
      case 11: X(-0.005); I(0.01); break;
      case 12: X(-0.008); I(0.005); I(0.012); break;
    }

    return ng;
  }

  for (var h = 1; h <= 12; h++) {
    var a = PI / 2 - h * PI / 6;
    var n = buildNumeral(h);
    n.position.set(NR * Math.cos(a), NR * Math.sin(a), fz + 0.002);
    n.rotation.z = -h * PI / 6;
    group.add(n);
  }

  for (var i = 0; i < 60; i++) {
    var hr = i % 5 === 0;
    var tl = hr ? 0.013 : 0.006;
    var tw = hr ? 0.002 : 0.001;
    var ta = PI / 2 - i * PI / 30;
    var tr = FR - 0.003 - tl / 2;
    var tick = new THREE.Mesh(
      new THREE.BoxGeometry(tw, tl, bd),
      darkMat
    );
    tick.position.set(tr * Math.cos(ta), tr * Math.sin(ta), fz + 0.002);
    tick.rotation.z = ta - PI / 2;
    group.add(tick);
  }

  var hLen = FR * 0.5;
  var hourGroup = new THREE.Group();
  var hHand = new THREE.Mesh(new THREE.BoxGeometry(0.01, hLen, 0.004), handMat);
  hHand.position.y = hLen / 2;
  hourGroup.add(hHand);
  var hTail = new THREE.Mesh(new THREE.BoxGeometry(0.013, hLen * 0.2, 0.004), handMat);
  hTail.position.y = -hLen * 0.1;
  hourGroup.add(hTail);
  hourGroup.rotation.z = -(10 + 10 / 60) / 12 * 2 * PI;
  hourGroup.position.z = fz + 0.004;
  group.add(hourGroup);

  var mLen = FR * 0.68;
  var minGroup = new THREE.Group();
  var mHand = new THREE.Mesh(new THREE.BoxGeometry(0.006, mLen, 0.003), handMat);
  mHand.position.y = mLen / 2;
  minGroup.add(mHand);
  var mTail = new THREE.Mesh(new THREE.BoxGeometry(0.009, mLen * 0.15, 0.003), handMat);
  mTail.position.y = -mLen * 0.075;
  minGroup.add(mTail);
  minGroup.rotation.z = -(10 / 60) * 2 * PI;
  minGroup.position.z = fz + 0.006;
  group.add(minGroup);

  var pin = new THREE.Mesh(
    new THREE.CylinderGeometry(0.01, 0.01, 0.008, 16),
    goldMat
  );
  pin.rotation.x = PI / 2;
  pin.position.z = fz + 0.005;
  group.add(pin);

  var dR = 0.014;
  var dGeo = new THREE.SphereGeometry(dR, 16, 12);
  for (var di = -1; di <= 1; di++) {
    var deco = new THREE.Mesh(dGeo, goldMat);
    deco.position.set(di * 0.035, OR + dR * 0.5, 0);
    group.add(deco);
  }

  return group;
}
