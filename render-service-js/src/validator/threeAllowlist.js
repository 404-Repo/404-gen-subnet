/**
 * THREE namespace member allowlist.
 *
 * Mirrors the "Allowed Three.js APIs" section of output_specifications.md.
 * Any `THREE.X` access where `X` is not in this set is rejected with rule
 * code FORBIDDEN_THREE_API.
 *
 * Edits here must be reflected in output_specifications.md (and vice versa).
 * In a production build this set should be generated from the markdown at
 * pipeline build time so the spec is the single source of truth.
 */

export const THREE_ALLOWED = new Set([
  // Geometry
  'BufferGeometry',
  'BufferAttribute',
  'InterleavedBuffer',
  'InterleavedBufferAttribute',
  'Float32BufferAttribute',
  'Uint8BufferAttribute',
  'Uint16BufferAttribute',
  'Uint32BufferAttribute',
  'Int8BufferAttribute',
  'Int16BufferAttribute',
  'Int32BufferAttribute',

  'BoxGeometry',
  'SphereGeometry',
  'CylinderGeometry',
  'ConeGeometry',
  'TorusGeometry',
  'TorusKnotGeometry',
  'PlaneGeometry',
  'CircleGeometry',
  'RingGeometry',
  'TetrahedronGeometry',
  'OctahedronGeometry',
  'DodecahedronGeometry',
  'IcosahedronGeometry',
  'PolyhedronGeometry',
  'ExtrudeGeometry',
  'LatheGeometry',
  'ShapeGeometry',
  'TubeGeometry',
  'EdgesGeometry',
  'WireframeGeometry',

  // Materials
  'MeshStandardMaterial',
  'MeshPhysicalMaterial',
  'MeshBasicMaterial',
  'PointsMaterial',
  'LineBasicMaterial',
  'LineDashedMaterial',

  // Textures
  'DataTexture',

  // Math
  'Vector2',
  'Vector3',
  'Vector4',
  'Matrix3',
  'Matrix4',
  'Quaternion',
  'Euler',
  'Box3',
  'Sphere',
  'Plane',
  'Ray',
  'Color',
  'MathUtils',

  // Curves & shapes
  'Curve',
  'CurvePath',
  'Shape',
  'Path',
  'CatmullRomCurve3',
  'CubicBezierCurve3',
  'LineCurve3',
  'QuadraticBezierCurve3',

  // Objects
  'Object3D',
  'Group',
  'Mesh',
  'InstancedMesh',
  'Line',
  'LineSegments',
  'Points',

  // Constants — color spaces
  'SRGBColorSpace',
  'LinearSRGBColorSpace',
  'NoColorSpace',

  // Constants — sides
  'FrontSide',
  'BackSide',
  'DoubleSide',

  // Constants — blending
  'NormalBlending',
  'AdditiveBlending',
  'SubtractiveBlending',
  'MultiplyBlending',
  'NoBlending',

  // Constants — texture filters
  'NearestFilter',
  'LinearFilter',
  'NearestMipmapNearestFilter',
  'LinearMipmapNearestFilter',
  'NearestMipmapLinearFilter',
  'LinearMipmapLinearFilter',

  // Constants — texture wrapping
  'RepeatWrapping',
  'ClampToEdgeWrapping',
  'MirroredRepeatWrapping',

  // Constants — texture formats and types
  'RGBAFormat',
  'RGBFormat',
  'RedFormat',
  'UnsignedByteType',
  'FloatType',
]);

/**
 * Submembers of allowed THREE.X members that are themselves blocked.
 * E.g. THREE.MathUtils is allowed, but THREE.MathUtils.seededRandom is not.
 */
export const THREE_BLOCKED_SUBMEMBERS = {
  MathUtils: new Set(['seededRandom', 'generateUUID']),
};
