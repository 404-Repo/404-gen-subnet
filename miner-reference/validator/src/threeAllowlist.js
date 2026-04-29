/**
 * THREE namespace member allowlist.
 *
 * Mirrors the "Allowed Three.js APIs" section of output_specifications.md.
 * Any `THREE.X` access where `X` is not in this set is rejected — with rule
 * code `FORBIDDEN_THREE_API` if `X` is a real Three.js member deliberately
 * excluded (see `THREE_DISALLOWED_KNOWN`), or `UNKNOWN_THREE_API` otherwise.
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
  'CapsuleGeometry',
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
  'Box2',
  'Box3',
  'Sphere',
  'Plane',
  'Ray',
  'Line3',
  'Triangle',
  'Spherical',
  'Cylindrical',
  'Color',
  'MathUtils',

  // Curves & shapes
  'Curve',
  'CurvePath',
  'Shape',
  'Path',
  // 3D curves
  'CatmullRomCurve3',
  'CubicBezierCurve3',
  'LineCurve3',
  'QuadraticBezierCurve3',
  // 2D curves (siblings used inside Shape / Path)
  'EllipseCurve',
  'ArcCurve',
  'LineCurve',
  'SplineCurve',
  'QuadraticBezierCurve',
  'CubicBezierCurve',

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
  'RGFormat',
  'RedFormat',
  'LuminanceFormat',
  'UnsignedByteType',
  'HalfFloatType',
  'FloatType',

  // Constants — buffer usage hints (harmless perf flags for setUsage())
  'StaticDrawUsage',
  'DynamicDrawUsage',
  'StreamDrawUsage',
]);

/**
 * Submembers of allowed THREE.X members that are themselves blocked.
 * E.g. THREE.MathUtils is allowed, but THREE.MathUtils.seededRandom is not.
 */
export const THREE_BLOCKED_SUBMEMBERS = {
  MathUtils: new Set(['seededRandom', 'generateUUID']),
};

/**
 * Real Three.js top-level members that are deliberately NOT on the allowlist.
 *
 * When static analysis sees `THREE.X` where X is not in THREE_ALLOWED but
 * IS in this set, the rejection uses rule `FORBIDDEN_THREE_API` — meaning
 * "this is a real Three.js API, we know about it, and we chose to exclude it."
 *
 * When X is in neither THREE_ALLOWED nor this set, rule `UNKNOWN_THREE_API`
 * is emitted instead — meaning "this is not a member of the Three.js
 * namespace; you probably hallucinated it."
 *
 * Grouped by reason for exclusion.
 */
export const THREE_DISALLOWED_KNOWN = new Set([
  // Lights — renderer controls lighting
  'AmbientLight',
  'DirectionalLight',
  'PointLight',
  'SpotLight',
  'HemisphereLight',
  'RectAreaLight',
  'LightProbe',
  'AmbientLightProbe',
  'HemisphereLightProbe',
  'DirectionalLightShadow',
  'PointLightShadow',
  'SpotLightShadow',
  'LightShadow',

  // Loaders — no network / filesystem access in sandbox.
  // Core loaders (top-level THREE members):
  'Loader',
  'LoadingManager',
  'FileLoader',
  'TextureLoader',
  'ImageLoader',
  'ImageBitmapLoader',
  'CubeTextureLoader',
  'DataTextureLoader',
  'BufferGeometryLoader',
  'MaterialLoader',
  'ObjectLoader',
  'FontLoader',
  'AudioLoader',
  'AnimationLoader',
  'CompressedTextureLoader',
  // Non-core loaders (live in `three/examples/jsm/loaders/` — NOT real
  // top-level `THREE.X` members, but miners frequently try them as such).
  // Treat as "known forbidden" rather than "unknown" for clearer errors.
  'GLTFLoader',
  'OBJLoader',
  'MTLLoader',
  'FBXLoader',
  'ColladaLoader',
  'STLLoader',
  'PLYLoader',
  '3MFLoader',
  'DRACOLoader',
  'KTX2Loader',
  'KTXLoader',
  'RGBELoader',
  'EXRLoader',
  'HDRCubeTextureLoader',
  'BasisTextureLoader',
  'TGALoader',
  'TIFFLoader',
  'SVGLoader',
  'USDZLoader',
  'VRMLLoader',
  'XYZLoader',

  // Materials not on the allowlist
  'ShaderMaterial',
  'RawShaderMaterial',
  'MeshPhongMaterial',
  'MeshLambertMaterial',
  'MeshToonMaterial',
  'MeshNormalMaterial',
  'MeshMatcapMaterial',
  'MeshDepthMaterial',
  'MeshDistanceMaterial',
  'ShadowMaterial',
  'SpriteMaterial',
  'Material',

  // Textures requiring DOM or compressed formats
  'Texture',
  'CanvasTexture',
  'VideoTexture',
  'CompressedTexture',
  'CompressedArrayTexture',
  'CompressedCubeTexture',
  'CubeTexture',
  'Data3DTexture',
  'DataArrayTexture',
  'DepthTexture',
  'FramebufferTexture',

  // Renderers / render targets / cameras — the validator runs the scene
  'WebGLRenderer',
  'WebGL1Renderer',
  'WebGLRenderTarget',
  'WebGL3DRenderTarget',
  'WebGLArrayRenderTarget',
  'WebGLCubeRenderTarget',
  'WebGLMultipleRenderTargets',
  'Camera',
  'PerspectiveCamera',
  'OrthographicCamera',
  'ArrayCamera',
  'StereoCamera',
  'CubeCamera',
  'Scene',
  'Fog',
  'FogExp2',

  // Animation — not applicable to static scenes
  'AnimationMixer',
  'AnimationAction',
  'AnimationClip',
  'AnimationObjectGroup',
  'AnimationUtils',
  'KeyframeTrack',
  'NumberKeyframeTrack',
  'VectorKeyframeTrack',
  'QuaternionKeyframeTrack',
  'ColorKeyframeTrack',
  'StringKeyframeTrack',
  'BooleanKeyframeTrack',
  'PropertyBinding',
  'PropertyMixer',

  // Skinning / rigging
  'Bone',
  'Skeleton',
  'SkinnedMesh',
  'SkeletonHelper',

  // Audio
  'Audio',
  'PositionalAudio',
  'AudioAnalyser',
  'AudioContext',
  'AudioListener',

  // Sprites / LOD / batching — not part of the minimal generator API
  'Sprite',
  'LOD',
  'BatchedMesh',

  // Helpers / debug visuals
  'ArrowHelper',
  'AxesHelper',
  'BoxHelper',
  'Box3Helper',
  'CameraHelper',
  'GridHelper',
  'PolarGridHelper',
  'HemisphereLightHelper',
  'DirectionalLightHelper',
  'PointLightHelper',
  'SpotLightHelper',
  'PlaneHelper',

  // Interaction / instrumentation — not needed for generation
  'Raycaster',
  'Clock',
  'EventDispatcher',
  'Layers',
]);
