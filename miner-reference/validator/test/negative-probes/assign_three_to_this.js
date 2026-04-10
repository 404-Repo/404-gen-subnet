// @expectedRule THREE_ALIAS_FORBIDDEN
// Stashing THREE on an instance field inside a class constructor. The
// subsequent `new this.t.ShaderMaterial()` dereferences a non-`THREE`
// identifier, so the THREE.X allowlist check would never fire — the
// analyzer explicitly rejects MemberExpression LHS assignments.
export default function generate(THREE) {
  class Builder {
    constructor(THREE) {
      this.t = THREE;
    }
    build() {
      return new this.t.Mesh(
        new this.t.BoxGeometry(0.1, 0.1, 0.1),
        new this.t.MeshStandardMaterial(),
      );
    }
  }
  return new Builder(THREE).build();
}
