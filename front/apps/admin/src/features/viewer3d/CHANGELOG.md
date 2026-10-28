# viewer3d 변경 이력

공개 입구(`index.ts`)와 그 타입을 바꾸면 여기에 적는다. 깨지는 변경은 FE 리뷰를 받은 뒤에만 한다.

## S15P31A101-186 PLANNED 가상 배치

모두 더하기이며 하위 호환이다.

- 추가: `plannedLayout` prop, `onPlannedEditPreview`·`onPlannedEditResult`, 선택 명령 `beginPlannedCreate`·`cancelPlannedEdit`·`finishPlannedEdit`·`updatePlannedEditField`.
- 추가: 대상 종류 `planned`(VIEWER_TARGET_TYPES), `PLANNED_OBB_POLICY`, `PLANNED_KINDS`·`readPlannedLayout`·`plannedGeometryProblem`, `ViewerPlanned*`·`Planned*` 타입.
- 바뀜(호환): `ViewerTargetType` 에 `planned` 가 늘었다. FE 코드에 종류별 완전 분기는 없다(dev 기준 확인). `createViewerTestHandle` 이 새 명령을 갖는다.
- 그대로: LIVE 작업면·장애물 편집 결과(`onObjectEdit*`)와 그 명령은 같은 모양이고 가상 객체 결과를 받지 않는다.

## S15P31A101-234 Spatial 독립 기반

모두 더하기이며 하위 호환이다. 계약 설명은 [CONTRACT.md](./CONTRACT.md)에 있다.

- 추가: BE 정식 구역(`GET /api/zones` 배열) 입력. BOX·PRISM·FOOTPRINT 로 표현하고 `ZoneView.shape`·`official` 을 둔다. 공개 함수는 `adaptZones`·`adaptOfficialZones`·`readOfficialZone`·`readPolygonXy`·`polygonArea` 다.
- 추가: BE 정식 정적 객체(`GET /api/static-objects` 배열) 입력. WORK_SURFACE·OBSTACLE 모두 받고 `objectVersions` 를 싣는다. 공개 함수는 `adaptGeometry`·`adaptOfficialStaticObjects` 다.
- 추가: `ExternalSpatialSource` 의 형상 보관·재전송, `clearSnapshot`, `setScope`·`scope`, `snapshotInfo`, push 옵션(`requestedAt`·`scope`)과 결과(`SnapshotPushOutcome`). 받는 쪽 `SpatialSourceSink.onSnapshotClear` 도 추가했다.
- 추가: `select(target, { waitMs, focus })`, `onSelectRequestChange`, `MAX_SELECT_WAIT_MS`.
- 추가: `beginEdit(target, { draft, before })` 로 초안을 한 번에 다시 연다(`ViewerBeginEditOptions`). 출처에는 `zoneVersion`·`objectVersion` 을 싣는다(정식 입력일 때만).
- 추가: `layers` prop(`ViewerLayers`, `VIEWER_LAYER_KEYS`·`VIEWER_LAYER_MODES`·`DEFAULT_LAYERS`).
- 추가: `personModelUrl` 공개 prop, `createViewerTestHandle`.
- 추가 export: `OBSTACLE_OBB_POLICY`, `ViewerObstacleGeometry`, `ViewerObjectKind`, `VIEWER_EDIT_REJECT_REASONS`.
- 바뀜(호환): `pushZones`·`pushWorkSurfaces` 반환형이 `void` 에서 `SnapshotPushOutcome` 이 됐다. `ZoneSnapshot.contract`·`zoneContract`·`geometryContract` 의 값 종류가 늘었다. `CommandResult` 성공 값에 선택 필드 `pending` 이 생겼다.
- 그대로: DEMO 구역·구성 묶음 입력의 결과, 기존 명령·콜백, 피킹 순서, `restoreZoneBoxSteps`(폐기는 FE 전환 뒤 따로 예고한다).

### MR !230 보완

- 고침: `objectVersions` 를 실제로 그린 항목의 행에서 읽는다. 같은 ID 의 앞 행이 빠지거나 작업면·장애물이 같은 ID 일 때 형상과 버전이 다른 행에서 나오던 문제다.
- 바뀜: 새 구독자는 마지막으로 받은 응답을 받는다(실패 뒤 이전 정상 형상을 다시 보내지 않음). `SourceSnapshotInfo.lastOkReceivedAt` 을 추가했다.
- 바뀜: 범위를 정한 Source 는 scope 없는 응답을 버린다. 비운 시각보다 앞서 요청한 응답(`requestedAt`)도 버린다.
