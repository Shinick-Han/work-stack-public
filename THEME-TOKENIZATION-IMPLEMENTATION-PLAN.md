# Work Stack 테마 토큰화 구현 계획

- 작성일: 2026-09-01
- 대상 브랜치: `codex/workstack-python-desktop-20260831`
- 상태: 구현 및 전체 회귀 검증 완료
- 대상 테마: `dark`, `light`

## 구현 결과 (2026-09-01)

- `theme/theme-tokens.json`을 사람이 수정하는 유일한 색상 원본으로 확정했다.
- 생성기가 웹 CSS와 Python 네이티브 팔레트를 함께 만들며, 산출물 drift를 CI에서 차단한다.
- 제품 CSS, Workspace CSS, Graph, MiniMap, Treemap 및 Windows 네이티브 surface의 직접 색상 리터럴을 0건으로 줄였다.
- 컴포넌트별 light override와 임시 legacy alias를 제거했다.
- 마지막 테마를 제한된 strict JSON으로 원자 저장하고, 시작 splash·복구 화면·네이티브 title bar부터 첫 프레임에 복원한다.
- dark/light 토큰 대비, 주요 화면 테마 전환, Graph/MiniMap 재색상, axe 접근성 검사를 release regression에 추가했다.
- 최종 검증: Python 568개 통과(1개 skip), frontend unit 339개 통과, Playwright E2E 39개 통과, production build 및 quality gate 통과.

## 1. 목적

Work Stack의 모든 제품 UI 색상을 중앙 테마 토큰으로 통합한다. 런타임에서는 `document.documentElement.dataset.theme` 하나만 변경해 전체 웹 UI, Graph/Treemap, Windows 네이티브 상단바와 Microsoft 로그인 오버레이가 같은 테마로 전환되게 한다.

이번 작업은 단순히 현재 보이는 light 테마 결함을 덮는 패치가 아니다. 새 컴포넌트가 추가되어도 컴포넌트별 light/dark 예외 규칙을 작성하지 않도록 색상 구조 자체를 바꾸는 것이 목표다.

## 2. 완료 조건

다음 조건을 모두 만족하면 테마 토큰화가 완료된 것으로 본다.

1. 테마 전환의 유일한 런타임 입력은 `dark | light` 값이다.
2. 제품 컴포넌트는 직접 색상값 대신 semantic token만 사용한다.
3. `:root[data-theme='light'] .some-component` 형태의 컴포넌트별 보정 규칙은 제거한다.
4. Graph, MiniMap, Treemap 및 SVG 요소도 테마 변경 즉시 갱신된다.
5. Windows 상단바와 네이티브 로그인 오버레이도 동일한 테마 입력을 사용한다.
6. 허용 목록에 없는 신규 `#hex`, `rgb()`, `hsl()` 직접 색상은 CI에서 차단된다.
7. 주요 화면에 대해 dark/light 시각 회귀 테스트를 통과한다.
8. 테마 전환 후 앱 재시작 없이 모든 표시 영역이 즉시 바뀐다.

## 3. 현재 상태와 문제

현재 `applyTheme()`은 이미 올바른 단일 스위치를 제공한다.

```ts
document.documentElement.dataset.theme = theme
```

그러나 색상 소비 방식은 다음 세 종류가 혼재한다.

- CSS 루트 변수 사용
- 파일 하단의 컴포넌트별 light override
- CSS, TSX, Python에 직접 작성된 색상값

2026-09-01 기준 1차 정적 조사에서는 토큰 선언부와 light override 영역을 제외하고도 다음 직접 색상 사용을 확인했다.

| 영역 | 직접 색상 사용 수 | 주요 위험 |
| --- | ---: | --- |
| `frontend/src/styles.css` 제품 컴포넌트 | 480 | 일부 컴포넌트가 light 테마에서도 다크 표면 유지 |
| Workspace view CSS | 127 | Graph/Board/Table 간 테마 품질 편차 |
| 런타임 TS/TSX | 26 | MiniMap, Graph, Treemap 내부 SVG가 CSS override를 무시 |

이 숫자는 브랜드색과 상태 의미색도 포함하므로 모든 항목이 결함이라는 뜻은 아니다. 다만 유지해야 할 고정색도 직접 리터럴이 아니라 명명된 토큰으로 이동한다.

## 4. 설계 원칙

### 4.1 하나의 테마 입력, 여러 semantic token

“변수 하나로 제어한다”는 것은 모든 요소를 하나의 색으로 만드는 것이 아니라, 테마 ID 하나가 전체 semantic token 집합을 선택한다는 의미다.

```css
:root,
:root[data-theme='dark'] {
  --ws-bg-app: #090a0e;
  --ws-surface-card: #111319;
  --ws-text-primary: #f2f3f5;
}

:root[data-theme='light'] {
  --ws-bg-app: #f3f5f8;
  --ws-surface-card: #ffffff;
  --ws-text-primary: #17202b;
}

.task-card {
  background: var(--ws-surface-card);
  color: var(--ws-text-primary);
}
```

### 4.2 색상 이름이 아니라 역할로 명명

`--dark-gray`, `--light-gray` 같은 색상명은 사용하지 않는다. 컴포넌트가 필요로 하는 역할을 기준으로 명명한다.

- 좋은 예: `--ws-surface-elevated`, `--ws-text-muted`, `--ws-status-danger-text`
- 피할 예: `--ws-gray-900`, `--ws-light-background`, `--ws-green-text`

Primitive palette가 필요한 경우 canonical theme 자료 내부에서만 사용하고 제품 CSS에는 노출하지 않는다.

### 4.3 컴포넌트별 테마 분기 금지

컴포넌트 CSS는 현재 테마가 무엇인지 몰라야 한다.

```css
/* 제거 대상 */
:root[data-theme='light'] .task-card { background: #fff; }

/* 목표 */
.task-card { background: var(--ws-surface-card); }
```

### 4.4 브랜드색과 의미색도 토큰화

두 테마에서 같은 값을 사용하는 라임 로고, P0-P3, 관계 유형 색상도 토큰으로 선언한다. 이를 통해 브랜드 조정, 고대비 대응, 그래프 범례와 실제 노드 간 색상 동기화를 한 곳에서 처리한다.

### 4.5 외부 콘텐츠는 제품 테마와 분리

Outlook, Teams, OneNote WebView 내부 콘텐츠는 Microsoft가 관리하는 별도 surface다. Work Stack은 WebView 컨테이너, 탭, 툴바, 로딩 상태만 토큰화하며 외부 페이지의 색상을 강제로 변조하지 않는다.

## 5. 토큰 계층

### 5.1 기본 surface와 텍스트

```text
--ws-bg-app
--ws-bg-sidebar
--ws-bg-canvas
--ws-surface-base
--ws-surface-raised
--ws-surface-overlay
--ws-surface-hover
--ws-surface-selected

--ws-border-subtle
--ws-border-default
--ws-border-strong

--ws-text-primary
--ws-text-secondary
--ws-text-muted
--ws-text-disabled
--ws-text-on-accent
```

### 5.2 상호작용과 그림자

```text
--ws-control-bg
--ws-control-bg-hover
--ws-control-border
--ws-control-border-focus
--ws-focus-ring
--ws-selection-bg
--ws-selection-border

--ws-shadow-sm
--ws-shadow-md
--ws-shadow-lg
--ws-backdrop
```

### 5.3 상태 토큰

각 상태는 최소한 surface, border, text 세 토큰을 갖는다.

```text
--ws-status-success-surface / border / text
--ws-status-info-surface / border / text
--ws-status-warning-surface / border / text
--ws-status-danger-surface / border / text
--ws-status-neutral-surface / border / text
```

Pill, toast, inline error, SSOT 연결 상태, 동기화 경고는 이 토큰을 공유한다.

### 5.4 Graph와 데이터 시각화

```text
--ws-graph-canvas
--ws-graph-grid
--ws-graph-edge-label-bg
--ws-graph-edge-label-text
--ws-graph-node-bg
--ws-graph-node-border
--ws-graph-node-selected
--ws-minimap-bg
--ws-minimap-mask
--ws-minimap-mask-border

--ws-relation-alignment
--ws-relation-dependency
--ws-relation-parent
--ws-relation-reference

--ws-priority-p0-surface / border / text
--ws-priority-p1-surface / border / text
--ws-priority-p2-surface / border / text
--ws-priority-p3-surface / border / text
```

## 6. Canonical source와 생성물

웹 CSS와 Python 네이티브 UI의 색상 drift를 막기 위해 다음 구조를 사용한다.

```text
theme/
  theme-tokens.json                 # 사람이 수정하는 유일한 색상 원본
scripts/
  generate-theme-tokens.mjs        # CSS/Python 생성기
frontend/src/generated/
  theme-tokens.css                 # 생성물, 직접 수정 금지
desktop/python-webview-shell/generated/
  theme_tokens.py                  # 생성물, 직접 수정 금지
```

`theme-tokens.json`은 `dark`와 `light`가 동일한 token key를 갖도록 한다. 생성기는 키 누락과 잘못된 색상 형식을 실패 처리한다.

```json
{
  "dark": {
    "bg.app": "#090a0e",
    "surface.card": "#111319",
    "text.primary": "#f2f3f5"
  },
  "light": {
    "bg.app": "#f3f5f8",
    "surface.card": "#ffffff",
    "text.primary": "#17202b"
  }
}
```

브라우저 런타임은 생성된 CSS를 사용한다. Python은 생성된 `THEME_TOKENS` 매핑에서 네이티브 caption, border, text, overlay 색상을 읽는다.

## 7. 구현 단계

### Phase 0 — 기준선과 안전장치

1. 직접 색상 인벤토리 스크립트를 추가한다.
2. 현재 허용 항목을 baseline allowlist로 저장한다.
3. Workspace, Focus, Context Inbox, Daily Review, Objective Hub, dialogs의 dark/light 기준 스크린샷을 만든다.
4. 현재 테마 unit test를 유지하고 token completeness test를 추가한다.

이 단계에서는 기존 색상을 차단하지 않고 신규 직접 색상 증가만 막는다. 마이그레이션 중 개발을 불필요하게 막지 않기 위한 조치다.

### Phase 1 — Canonical token 기반 구축

1. `theme/theme-tokens.json` 작성
2. CSS/Python 생성기 추가
3. 생성물 최신 여부를 검사하는 `check` 모드 추가
4. `styles.css`의 기존 루트 변수와 새 토큰을 임시 alias로 연결

```css
--bg: var(--ws-bg-app);
--panel: var(--ws-surface-base);
--text: var(--ws-text-primary);
```

기존 UI를 깨지 않고 점진적으로 이름을 교체한다.

### Phase 2 — 공통 제품 UI 마이그레이션

다음 순서로 직접 색상을 semantic token으로 교체한다.

1. App shell, sidebar, topbar
2. 버튼, 입력창, 검색, tab, menu
3. card, dialog, drawer, toast
4. status pill, error, warning, success
5. SSOT 연결 및 synchronization UI
6. Context Inbox와 source capture UI

각 묶음이 끝날 때 dark/light 스크린샷을 비교한다. 한 번에 480개를 치환한 뒤 문제를 찾는 방식은 사용하지 않는다.

### Phase 3 — Workspace view 마이그레이션

1. `workspace-views.css`의 별도 `--wsv-*` 토큰을 `--ws-*` semantic token alias로 연결한다.
2. Board, Table, Graph node, legend, controls를 교체한다.
3. Workspace 전용 light component override를 제거한다.
4. 선택, hover, focus, drag 상태를 양쪽 테마에서 검증한다.

### Phase 4 — Graph, MiniMap, Treemap 런타임 색상

1. React Flow가 CSS custom property 문자열을 각 color prop에서 정상 수용하는지 작은 테스트로 확인한다.
2. 지원하는 속성은 `var(--ws-...)`를 직접 전달한다.
3. 라이브러리가 CSS 변수를 받지 못하는 속성만 `getComputedStyle()` 기반 `readThemeToken()` helper를 사용한다.
4. helper를 사용하는 컴포넌트는 `theme` 변경 시 다시 계산되도록 한다.
5. Treemap SVG fill/stroke와 Graph edge label을 토큰으로 교체한다.

### Phase 5 — Windows 네이티브 UI

1. 상단바 색상을 생성된 Python token map에 연결한다.
2. Microsoft 로그인 popup overlay와 toolbar를 동일한 map에 연결한다.
3. WebView의 외부 콘텐츠 기본 배경은 별도의 `--external-content-loading-bg` 역할로 관리한다.
4. `workstack-window-theme|dark|light` 메시지 한 번으로 열린 네이티브 surface를 모두 갱신한다.

### Phase 6 — 예외 제거와 정리

1. `:root[data-theme='light'] .component` 규칙을 제거한다.
2. 기존 호환 alias를 제거한다.
3. 직접 색상 allowlist를 브랜드 asset, 외부 콘텐츠 호환값 등 최소 범위로 축소한다.
4. 사용되지 않는 토큰을 제거한다.
5. 테마 문서와 컴포넌트 작성 규칙을 갱신한다.

## 8. 검증 전략

### 8.1 정적 검사

- `theme-tokens.json`의 dark/light key 집합이 동일한지 검사
- 생성된 CSS/Python이 최신인지 검사
- 제품 코드의 직접 `hex/rgb/hsl` 사용 검사
- 허용 목록에는 사유와 만료 조건을 기록

### 8.2 Unit/component test

- 저장된 테마 복원
- 테마 변경 시 `data-theme`, `color-scheme`, localStorage 동기화
- 네이티브 WebView 메시지 전송
- Graph/Treemap token resolver가 테마 변경 후 새 값을 반환
- 상태별 token 세트에 surface/border/text 누락이 없는지 검사

### 8.3 시각 회귀 테스트

다음 화면을 동일한 데이터와 viewport에서 dark/light 각각 캡처한다.

- Workspace: Graph, Board, Table, Treemap
- Focus
- Context Inbox 및 Microsoft source dock
- Daily Review
- Objective Hub
- Task drawer
- Connection center
- Update dialog
- Source review dialog
- 오류, 경고, 빈 상태, toast

픽셀 차이는 자동 비교하되, 첫 마이그레이션에서는 새 기준 이미지 승인을 사람이 한 번 수행한다.

### 8.4 접근성 검사

- 일반 텍스트: WCAG AA 4.5:1 목표
- 큰 텍스트 및 주요 그래픽: 3:1 목표
- focus ring은 모든 주요 surface에서 식별 가능해야 함
- 상태를 색상만으로 전달하지 않음

## 9. CI 및 Release regression flow

Release 검증 흐름에 다음 단계를 추가한다.

```text
theme token schema check
  -> generated files clean check
  -> direct color lint
  -> frontend unit/component tests
  -> dark visual regression
  -> light visual regression
  -> desktop native theme smoke
  -> installer/release smoke
```

초기 마이그레이션 중에는 기존 직접 색상 baseline을 초과할 때만 실패시킨다. Phase 6 이후에는 allowlist 외 직접 색상을 모두 실패 처리한다.

## 10. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 기계적 대량 치환으로 UI 위계가 평평해짐 | surface 역할을 먼저 정의하고 화면 묶음별로 마이그레이션 |
| light 테마에서 상태 텍스트 대비 저하 | 상태별 surface/border/text를 한 세트로 검증 |
| React Flow가 CSS 변수를 해석하지 못함 | 제한된 token resolver helper 사용 |
| CSS와 Python 색상 drift | 한 JSON에서 CSS와 Python 생성 |
| 시각 테스트가 작은 렌더링 차이로 불안정 | 고정 viewport/font/data와 영역 단위 캡처 사용 |
| lint가 개발을 과도하게 차단 | baseline 감소 방식으로 시작하고 Phase 6에서 강화 |

## 11. 작업 단위와 커밋 경계

권장 커밋 순서는 다음과 같다.

1. `chore(theme): add canonical tokens and generator`
2. `refactor(theme): migrate shared product surfaces`
3. `refactor(theme): migrate status and feedback colors`
4. `refactor(theme): migrate workspace views`
5. `refactor(theme): theme graph and treemap runtime colors`
6. `refactor(theme): unify native desktop palette`
7. `test(theme): add dual-theme visual regression`
8. `chore(theme): enforce direct color policy`

각 커밋은 독립적으로 build/test 가능해야 하며, dark 테마 외형을 가능한 한 유지한 상태에서 light 테마 누락을 줄인다.

## 12. 최종 산출물

- canonical `theme-tokens.json`
- CSS/Python token generator와 생성물
- 직접 색상 검사 스크립트와 최소 allowlist
- 토큰화된 제품 UI, Workspace view, Graph/Treemap, 네이티브 UI
- dark/light 시각 회귀 기준 이미지
- Release regression 단계
- 신규 UI 작성자를 위한 테마 토큰 사용 지침

## 13. 구현 시작 권장점

첫 구현 묶음은 Phase 0과 Phase 1로 제한한다. 이 단계에서 화면 외형을 바꾸지 않고 canonical token 기반, 생성기, baseline 검사까지 먼저 확보한다. 이후 공통 surface부터 작은 묶음으로 치환하면 기능 개발을 중단하지 않으면서 직접 색상 수를 지속적으로 줄일 수 있다.
