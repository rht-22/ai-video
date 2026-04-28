// Build AI_Video_Shorts_Creator.pptx — 14 slides, dark professional theme
const PptxGenJS = require('pptxgenjs');
const path = require('path');

const pptx = new PptxGenJS();
pptx.layout = 'LAYOUT_WIDE'; // 13.33 x 7.5

const C = {
  bgDark:    '0E1B2C',
  bgPanel:   '14253A',
  bgCard:    '1A2D45',
  bgCardLi:  '223552',
  accent:    '4FC3F7',
  accent2:   '02C39A',
  accent3:   'FFB454',
  textHi:    'F0F7FF',
  textMid:   'B5C7DB',
  textMuted: '7A8FA8',
  divider:   '2A3F5C',
};
const FH = 'Arial Black';
const FB = 'Calibri';
const FM = 'Consolas';

function dark() {
  const s = pptx.addSlide();
  s.background = { color: C.bgDark };
  return s;
}

function setHeader(s, title, sub) {
  s.addText(title, {
    x: 0.5, y: 0.4, w: 12.3, h: 0.7,
    fontFace: FH, fontSize: 32, bold: true, color: C.textHi, margin: 0,
  });
  if (sub) s.addText(sub, {
    x: 0.5, y: 1.05, w: 12.3, h: 0.4,
    fontFace: FB, fontSize: 14, color: C.textMid, italic: true, margin: 0,
  });
  s.addShape(pptx.ShapeType.rect, {
    x: 0.5, y: 1.55, w: 0.6, h: 0.06,
    fill: { color: C.accent }, line: { color: C.accent },
  });
}

function card(s, x, y, w, h, fill) {
  s.addShape(pptx.ShapeType.roundRect, {
    x, y, w, h,
    fill: { color: fill || C.bgCard },
    line: { color: C.divider, width: 0.75 },
    rectRadius: 0.08,
  });
}

function footer(s, num, total) {
  s.addText(`AI Video Shorts Creator   ·   ${num}/${total}`, {
    x: 0.5, y: 7.05, w: 12.3, h: 0.3,
    fontFace: FB, fontSize: 10, color: C.textMuted, align: 'right', margin: 0,
  });
}

const TOTAL = 14;

/* =========== Slide 1 — Title =========== */
{
  const s = dark();
  s.addShape(pptx.ShapeType.rect, {
    x: 0, y: 6.3, w: 13.33, h: 1.2, fill: { color: C.bgPanel }, line: { color: C.bgPanel },
  });
  // accent stripe
  s.addShape(pptx.ShapeType.rect, {
    x: 0.7, y: 1.8, w: 0.15, h: 3.2, fill: { color: C.accent }, line: { color: C.accent },
  });
  s.addText('AI Video Shorts', {
    x: 1.1, y: 1.7, w: 11.5, h: 1.2,
    fontFace: FH, fontSize: 60, bold: true, color: C.textHi, margin: 0,
  });
  s.addText('Creator', {
    x: 1.1, y: 2.7, w: 11.5, h: 1.2,
    fontFace: FH, fontSize: 60, bold: true, color: C.accent, margin: 0,
  });
  s.addText('16-stage 자동 쇼츠 파이프라인', {
    x: 1.1, y: 4.0, w: 11.5, h: 0.6,
    fontFace: FB, fontSize: 26, color: C.textMid, margin: 0,
  });
  s.addText('한국어 드라마 · 예능 영상  →  60초 세로 쇼츠 (9:16)', {
    x: 1.1, y: 4.7, w: 11.5, h: 0.5,
    fontFace: FB, fontSize: 18, color: C.textMuted, italic: true, margin: 0,
  });
  s.addText('Architecture & Pipeline Overview', {
    x: 0.7, y: 6.55, w: 12, h: 0.4,
    fontFace: FB, fontSize: 14, color: C.textMuted, margin: 0,
  });
  s.addText('Apr 2026', {
    x: 0.7, y: 6.95, w: 12, h: 0.3,
    fontFace: FB, fontSize: 11, color: C.textMuted, margin: 0,
  });
}

/* =========== Slide 2 — Overview =========== */
{
  const s = dark();
  setHeader(s, '프로젝트 개요', 'What it is  /  Inputs  /  Outputs');

  card(s, 0.5, 2.0, 6.0, 4.7);
  s.addText('한 문장', {
    x: 0.8, y: 2.15, w: 5.5, h: 0.35, fontFace: FB, fontSize: 13, color: C.accent, bold: true, margin: 0,
  });
  s.addText('한국어 드라마·예능 영상 한 편을\n받아 60초 9:16 세로 쇼츠를\n자동 제작하는 16단계 파이프라인.', {
    x: 0.8, y: 2.55, w: 5.5, h: 1.9,
    fontFace: FB, fontSize: 22, color: C.textHi, bold: true, valign: 'top', margin: 0,
  });
  s.addText('차별점', {
    x: 0.8, y: 4.55, w: 5.5, h: 0.3, fontFace: FB, fontSize: 13, color: C.accent, bold: true, margin: 0,
  });
  s.addText([
    {text: '• Gemini Pro 멀티모달 직접 분석', options: {breakLine: true}},
    {text: '• face_id 사전 인덱스로 인물·크롭 일관성', options: {breakLine: true}},
    {text: '• TTS cue 분리 (voice / speed / 위치 가변)', options: {breakLine: true}},
    {text: '• 16개 체크포인트 → 부분 재실행', options: {}},
  ], {
    x: 0.8, y: 4.85, w: 5.5, h: 1.7,
    fontFace: FB, fontSize: 14, color: C.textMid, valign: 'top', margin: 0,
  });

  card(s, 6.83, 2.0, 6.0, 4.7);
  s.addText('입력', {
    x: 7.13, y: 2.15, w: 5.5, h: 0.35, fontFace: FB, fontSize: 13, color: C.accent2, bold: true, margin: 0,
  });
  s.addText([
    {text: '• 원본 영상 (mp4)', options: {breakLine: true}},
    {text: '• 자막 SRT / ASS / VTT / SMI', options: {breakLine: true}},
    {text: '• 또는 YouTube URL (yt-dlp 자동 다운로드)', options: {breakLine: true}},
    {text: '• 작품명 · 주제(선택) · 이전 화 요약(선택)', options: {}},
  ], {
    x: 7.13, y: 2.5, w: 5.5, h: 1.8,
    fontFace: FB, fontSize: 15, color: C.textHi, valign: 'top', margin: 0,
  });
  s.addText('출력', {
    x: 7.13, y: 4.35, w: 5.5, h: 0.35, fontFace: FB, fontSize: 13, color: C.accent2, bold: true, margin: 0,
  });
  s.addText([
    {text: '• 9:16 세로 쇼츠 mp4 (1 ~ 3개)', options: {breakLine: true}},
    {text: '• edit_plan.json (편집 계획)', options: {breakLine: true}},
    {text: '• run_log.json + 16개 체크포인트', options: {breakLine: true}},
    {text: '• ASS 자막 파일 + cue별 TTS mp3', options: {}},
  ], {
    x: 7.13, y: 4.7, w: 5.5, h: 1.9,
    fontFace: FB, fontSize: 15, color: C.textHi, valign: 'top', margin: 0,
  });

  footer(s, 2, TOTAL);
}

/* =========== Slide 3 — Core Values (4 cards 2x2) =========== */
{
  const s = dark();
  setHeader(s, '핵심 가치', 'Why this pipeline');

  const cards = [
    {x: 0.5,  y: 2.0, color: C.accent,  title: 'Hands-off 60초 쇼츠',
     body: '영상 한 편 던지면 후킹·서사·자막·TTS·렌더까지\n전부 자동.'},
    {x: 6.83, y: 2.0, color: C.accent2, title: '재실행 친화',
     body: '16개 체크포인트로 어느 단계부터든\n--from-step + --job-id 로 부분 재실행.'},
    {x: 0.5,  y: 4.55, color: C.accent3, title: '비용 최적화',
     body: 'Pro는 청크 멀티모달 분석에만,\nFlash는 가벼운 의사결정에만 사용.'},
    {x: 6.83, y: 4.55, color: 'FF6B9D', title: '품질 보강',
     body: 'face_id 사전 인덱스로 인물 일관성,\nTTS 톤은 작품 분위기 매칭.'},
  ];
  cards.forEach((c, i) => {
    card(s, c.x, c.y, 6.0, 2.4);
    s.addShape(pptx.ShapeType.rect, {
      x: c.x, y: c.y, w: 0.12, h: 2.4, fill: { color: c.color }, line: { color: c.color },
    });
    s.addText(`0${i + 1}`, {
      x: c.x + 0.35, y: c.y + 0.25, w: 1.0, h: 0.5,
      fontFace: FH, fontSize: 24, color: c.color, bold: true, margin: 0,
    });
    s.addText(c.title, {
      x: c.x + 0.35, y: c.y + 0.78, w: 5.4, h: 0.5,
      fontFace: FH, fontSize: 22, color: C.textHi, bold: true, margin: 0,
    });
    s.addText(c.body, {
      x: c.x + 0.35, y: c.y + 1.3, w: 5.4, h: 1.0,
      fontFace: FB, fontSize: 14, color: C.textMid, valign: 'top', margin: 0,
    });
  });

  footer(s, 3, TOTAL);
}

/* =========== Slide 4 — Tech Stack =========== */
{
  const s = dark();
  setHeader(s, '기술 스택', 'Tech stack');

  const rows = [
    ['언어',        'Python 3.x'],
    ['LLM',         'Google Gemini API — gemini-3.1-pro-preview (Pro), gemini-3-flash-preview (Flash)'],
    ['영상 처리',    'ffmpeg / ffprobe  (filter_complex, libx264, hwaccel: d3d11va / cuda)'],
    ['얼굴 인식',    'deepface (ArcFace embedding)  +  OpenCV (Haar Cascade for crop)'],
    ['TTS',         'edge-tts — Edge Neural Voices (한국어 3 voice × pitch 변형)'],
    ['다운로드',     'yt-dlp (YouTube URL 입력 시 영상 + 자막 자동 다운로드)'],
    ['자막',        'SRT / ASS / VTT / SMI 파서  +  libass (ASS 렌더)'],
    ['전사 폴백',    'OpenAI Whisper (SRT 미제공 시)'],
  ];
  const startY = 2.0;
  const rowH = 0.55;
  rows.forEach((r, i) => {
    const y = startY + i * rowH;
    if (i % 2 === 0) {
      s.addShape(pptx.ShapeType.rect, {
        x: 0.5, y, w: 12.3, h: rowH, fill: { color: C.bgCard }, line: { color: C.bgCard },
      });
    }
    s.addText(r[0], {
      x: 0.7, y: y + 0.05, w: 2.5, h: rowH - 0.1,
      fontFace: FB, fontSize: 14, color: C.accent, bold: true, valign: 'middle', margin: 0,
    });
    s.addText(r[1], {
      x: 3.3, y: y + 0.05, w: 9.3, h: rowH - 0.1,
      fontFace: FB, fontSize: 14, color: C.textHi, valign: 'middle', margin: 0,
    });
  });

  footer(s, 4, TOTAL);
}

/* =========== Slide 5 — System Architecture =========== */
{
  const s = dark();
  setHeader(s, '시스템 아키텍처', 'Three logical phases');

  const cols = [
    {x: 0.5,  title: '입력 처리',  steps: ['1. init', '2. research', '3. probe', '4. proxy', '5. exclusion', '6. chunk'],
     color: C.accent,  caption: '메타데이터 + 분석용\n프록시 영상 준비'},
    {x: 4.78, title: 'AI 분석',    steps: ['7. skeleton', '8. character_index', '9. gemini', '10. graph', '11. story', '12. tts_plan'],
     color: C.accent2, caption: 'Pro 멀티모달 + Flash\n의사결정 파이프'},
    {x: 9.05, title: '합성 · 렌더', steps: ['13. transcribe', '14. resources', '15. render', '16. validate'],
     color: C.accent3, caption: 'TTS 합성 +\nffmpeg 최종 렌더'},
  ];
  cols.forEach((c) => {
    card(s, c.x, 2.0, 3.78, 4.7);
    s.addShape(pptx.ShapeType.rect, {
      x: c.x, y: 2.0, w: 3.78, h: 0.08, fill: { color: c.color }, line: { color: c.color },
    });
    s.addText(c.title, {
      x: c.x + 0.25, y: 2.2, w: 3.4, h: 0.5,
      fontFace: FH, fontSize: 22, color: C.textHi, bold: true, margin: 0,
    });
    s.addText(c.caption, {
      x: c.x + 0.25, y: 2.75, w: 3.4, h: 0.7,
      fontFace: FB, fontSize: 12, color: c.color, italic: true, valign: 'top', margin: 0,
    });
    c.steps.forEach((step, i) => {
      const y = 3.6 + i * 0.45;
      s.addShape(pptx.ShapeType.rect, {
        x: c.x + 0.25, y, w: 0.06, h: 0.35, fill: { color: c.color }, line: { color: c.color },
      });
      s.addText(step, {
        x: c.x + 0.45, y, w: 3.2, h: 0.35,
        fontFace: FB, fontSize: 14, color: C.textHi, valign: 'middle', margin: 0,
      });
    });
  });
  // Arrows between cols
  [4.32, 8.6].forEach((x) => {
    s.addText('▶', {
      x, y: 4.15, w: 0.4, h: 0.5,
      fontFace: FB, fontSize: 28, color: C.accent2, align: 'center', valign: 'middle', margin: 0,
    });
  });

  s.addText('체크포인트 (.json) — 어느 단계에서든 재시작 가능', {
    x: 0.5, y: 6.85, w: 12.3, h: 0.25,
    fontFace: FB, fontSize: 11, color: C.textMuted, italic: true, align: 'center', margin: 0,
  });

  footer(s, 5, TOTAL);
}

/* =========== Slide 6 — 16-stage map =========== */
{
  const s = dark();
  setHeader(s, '16단계 파이프라인', 'Pipeline at a glance');

  const stages = [
    [1, 'init',             C.accent ],
    [2, 'research',         C.accent ],
    [3, 'probe',            C.accent ],
    [4, 'proxy',            C.accent ],
    [5, 'exclusion',        C.accent ],
    [6, 'chunk',            C.accent ],
    [7, 'skeleton',         C.accent2],
    [8, 'character_index',  C.accent2],
    [9, 'gemini',           C.accent2],
    [10,'graph',            C.accent2],
    [11,'story',            C.accent2],
    [12,'tts_plan',         C.accent2],
    [13,'transcribe',       C.accent3],
    [14,'resources',        C.accent3],
    [15,'render',           C.accent3],
    [16,'validate',         C.accent3],
  ];
  const cols = 4, rows = 4;
  const cellW = 2.95, cellH = 1.18, gx = 0.55, gy = 0.18;
  const startX = 0.5, startY = 2.0;
  stages.forEach(([n, name, color], i) => {
    const cx = i % cols, cy = Math.floor(i / cols);
    const x = startX + cx * (cellW + gx);
    const y = startY + cy * (cellH + gy);
    card(s, x, y, cellW, cellH, C.bgCard);
    s.addShape(pptx.ShapeType.rect, {
      x, y, w: cellW, h: 0.06, fill: { color }, line: { color },
    });
    s.addText(String(n).padStart(2, '0'), {
      x: x + 0.2, y: y + 0.12, w: 1.0, h: 0.55,
      fontFace: FH, fontSize: 28, color, bold: true, margin: 0,
    });
    s.addText(name, {
      x: x + 0.2, y: y + 0.65, w: cellW - 0.4, h: 0.5,
      fontFace: FB, fontSize: 18, color: C.textHi, bold: true, valign: 'middle', margin: 0,
    });
  });

  footer(s, 6, TOTAL);
}

/* helper for stage detail slides */
function stageDetail(s, num, items) {
  // Each item: {n, name, role, io}
  const rowH = 0.85;
  items.forEach((it, i) => {
    const y = 2.0 + i * (rowH + 0.05);
    s.addShape(pptx.ShapeType.rect, {
      x: 0.5, y, w: 0.95, h: rowH,
      fill: { color: C.bgPanel }, line: { color: C.divider },
    });
    s.addText(String(it.n).padStart(2, '0'), {
      x: 0.5, y: y, w: 0.95, h: rowH,
      fontFace: FH, fontSize: 24, color: C.accent, bold: true, align: 'center', valign: 'middle', margin: 0,
    });
    card(s, 1.5, y, 11.33, rowH);
    s.addText(it.name, {
      x: 1.7, y: y + 0.05, w: 3.5, h: 0.4,
      fontFace: FH, fontSize: 16, color: C.textHi, bold: true, margin: 0,
    });
    s.addText(it.role, {
      x: 1.7, y: y + 0.4, w: 11.0, h: rowH - 0.45,
      fontFace: FB, fontSize: 12, color: C.textMid, valign: 'top', margin: 0,
    });
    if (it.io) s.addText(it.io, {
      x: 5.3, y: y + 0.05, w: 7.4, h: 0.35,
      fontFace: FM, fontSize: 11, color: C.accent2, italic: true, margin: 0,
    });
  });
  footer(s, num, TOTAL);
}

/* =========== Slide 7 — Stage detail 1: 입력 처리 =========== */
{
  const s = dark();
  setHeader(s, '단계 상세 1 — 입력 처리', 'Stages 1 to 6');
  stageDetail(s, 7, [
    {n:1, name:'init',      role:'job_id 생성, output_dir 초기화, run_log.json 시작.', io:'→ output_dir/run_log.json'},
    {n:2, name:'research',  role:'Flash로 작품 시놉시스·인물·회차 검색 + TMDb 배우 프로필 다운로드.', io:'→ checkpoint_research.json + _research/cast_*.jpg'},
    {n:3, name:'probe',     role:'ffprobe로 영상 메타데이터 (duration, w×h, fps, audio) 추출.', io:'→ checkpoint_probe.json'},
    {n:4, name:'proxy',     role:'480p / fps=4 mp4 인코딩 — Gemini 분석 비용 최소화. 한 번 만들면 재사용.', io:'→ <slug>_480.mp4'},
    {n:5, name:'exclusion', role:'인트로/엔딩 크레딧 자동 + 수동 감지로 제외 시간 범위 결정.', io:'→ checkpoint_exclusion.json'},
    {n:6, name:'chunk',     role:'프록시를 300초 단위(10초 overlap) 분할 → Gemini File API 업로드용 split mp4.', io:'→ chunk_*.mp4 in temp'},
  ]);
}

/* =========== Slide 8 — Stage detail 2: AI 분석 1 =========== */
{
  const s = dark();
  setHeader(s, '단계 상세 2 — AI 분석 (전반)', 'Stages 7 to 9');
  stageDetail(s, 8, [
    {n:7, name:'skeleton',         role:'Flash가 전체 영상을 한 번에 보고 의도 / 주요 장면 / has_intro / has_credits 메타 추출. 모든 chunk 분석에 컨텍스트로 재사용.', io:'→ narrative_skeleton.json'},
    {n:8, name:'character_index ★', role:'프록시를 2초당 1프레임 ArcFace 임베딩 → 인물별 등장 구간 + 정규화 좌표 인덱스. 후속 단계에서 lookup 으로 재사용 (ArcFace 호출 75% 절감).', io:'→ checkpoint_character_index.json'},
    {n:9, name:'gemini (Pro)',     role:'각 chunk 영상 + 자막 + character_appearances + skeleton + 이전 chunk 요약을 받아 segments[] (전체 묘사) + candidate_moments[] (쇼츠 후보) 추출. 점수 없이 description / highlight_eligible / continues_from 등 의미적 평가만.', io:'→ checkpoint_gemini.json'},
  ]);
}

/* =========== Slide 9 — Stage detail 3: AI 분석 2 =========== */
{
  const s = dark();
  setHeader(s, '단계 상세 3 — AI 분석 (후반)', 'Stages 10 to 12');
  stageDetail(s, 9, [
    {n:10, name:'graph',     role:'candidate들 사이의 관계 (setup_payoff / continuous / consequence / character_arc) 추출. assign_sequence_ids로 cross-chunk 연속 장면을 같은 sequence로 묶음.', io:'→ checkpoint_graph.json'},
    {n:11, name:'story (Flash)',    role:'candidates + edges + skeleton + 이전 화 컨텍스트 → storyline. storytelling (hook + build×N + payoff) 또는 highlight (단일 클립). 3개 storyline 생성 후 점수순 1~3위 채택.', io:'→ checkpoint_story.json'},
    {n:12, name:'tts_plan ★ (Flash)', role:'결정된 storyline 클립 시퀀스를 받아 편집 타임라인 절대 시간 기준 cue 리스트 생성. voice 4 / speed 5 프리셋. 톤: 격식체·슬랭 모두 금지, 명사형 종결 / 상황 설명 / 궁금증 유발.', io:'→ checkpoint_tts_plan.json'},
  ]);
}

/* =========== Slide 10 — Stage detail 4: 합성·렌더 =========== */
{
  const s = dark();
  setHeader(s, '단계 상세 4 — 합성 · 렌더', 'Stages 13 to 16');
  stageDetail(s, 10, [
    {n:13, name:'transcribe',  role:'선택된 클립 구간만 전사 (SRT 우선, 없으면 Whisper). 무음 ≥ 0.8s 자동 컷.', io:'→ full_audio.json'},
    {n:14, name:'resources',   role:'클립별 얼굴 크롭 타임라인 (build_crop_timeline + character_index lookup) + cue별 TTS mp3 합성.', io:'→ crop_*.json + tts_cue_*.mp3'},
    {n:15, name:'render',      role:'ffmpeg filter_complex: 클립 concat → blur 9:16 + 제목 + ASS 자막 + 원본 덕킹 + cue adelay + amix. GPU 폴백: d3d11va → cuda → libx264.', io:'→ shorts.mp4 (+ shorts_2/3 variants)'},
    {n:16, name:'validate',    role:'duration_ok / audio_peak_ok / black_frame_ok 검증. 실패 시 run_log에 경고 기록.', io:'→ run_log.steps[].validate'},
  ]);
}

/* =========== Slide 11 — AI Models =========== */
{
  const s = dark();
  setHeader(s, 'AI 모델 분담', 'Pro / Flash / Edge TTS / ArcFace');

  const rows = [
    ['gemini-3.1-pro-preview',  'Pro 멀티모달',   '9 (chunk)',                    '영상 + 자막 직접 보고 candidate_moments 추출'],
    ['gemini-3-flash-preview',  'Flash 텍스트',   '2 / 7 / 10 / 11 / 12',        'research · skeleton · graph · story · tts_plan'],
    ['Edge TTS (Microsoft)',    '한국어 3 voice', '14',                           'cue별 mp3 합성 (voice × pitch × rate)'],
    ['deepface (ArcFace)',       '얼굴 임베딩',   '8 / 14',                       '인물 등장 인덱스 + 크롭 타겟 lookup'],
    ['Whisper (선택)',           '전사 폴백',      '13',                           'SRT 미제공 시 자동 전사'],
    ['yt-dlp',                   '다운로더',       '입력',                         'YouTube URL → 영상 + 자막 자동 수집'],
  ];
  const headerY = 2.0;
  // header row
  s.addShape(pptx.ShapeType.rect, {
    x: 0.5, y: headerY, w: 12.3, h: 0.5, fill: { color: C.bgPanel }, line: { color: C.bgPanel },
  });
  ['Model / SDK', 'Type', 'Stages', 'Role'].forEach((h, i) => {
    const xs = [0.7, 4.0, 6.5, 8.5][i];
    const ws = [3.2, 2.4, 1.9, 4.2][i];
    s.addText(h, {
      x: xs, y: headerY + 0.05, w: ws, h: 0.4,
      fontFace: FB, fontSize: 13, color: C.accent, bold: true, valign: 'middle', margin: 0,
    });
  });
  rows.forEach((r, i) => {
    const y = headerY + 0.55 + i * 0.6;
    if (i % 2 === 0) {
      s.addShape(pptx.ShapeType.rect, {
        x: 0.5, y, w: 12.3, h: 0.55, fill: { color: C.bgCard }, line: { color: C.bgCard },
      });
    }
    [r[0], r[1], r[2], r[3]].forEach((cell, j) => {
      const xs = [0.7, 4.0, 6.5, 8.5][j];
      const ws = [3.2, 2.4, 1.9, 4.2][j];
      const isCode = j === 0;
      s.addText(cell, {
        x: xs, y: y + 0.05, w: ws, h: 0.45,
        fontFace: isCode ? FM : FB, fontSize: 12, color: isCode ? C.accent2 : C.textHi,
        valign: 'middle', margin: 0,
      });
    });
  });

  footer(s, 11, TOTAL);
}

/* =========== Slide 12 — Data Models =========== */
{
  const s = dark();
  setHeader(s, '핵심 데이터 모델', 'Frozen dataclasses & JSON schemas');

  const boxes = [
    {x: 0.5,  y: 2.0, title: 'StoryClip',           code: 'StoryClip(\n  role,\n  start_sec, end_sec,\n  subtitle,\n  use_original_audio,\n  chunk_index, candidate_index,\n  character_focus,\n  bridges_from_previous,\n)'},
    {x: 6.83, y: 2.0, title: 'TTSCue',              code: 'TTSCue(\n  start_sec, end_sec,\n  text,\n  voice="narrative_female",\n  speed="normal",\n)'},
    {x: 0.5,  y: 4.55, title: 'candidate_moment',   code: '{\n  chunk_index, candidate_index,\n  start_sec, end_sec,\n  characters_in_scene, character_focus,\n  description, transcript,\n  scene_location, timeline_position,\n  continues_from,\n  requires_context,\n  highlight_eligible, highlight_reason,\n}'},
    {x: 6.83, y: 4.55, title: 'character_appearance', code: '{\n  character: "최희로",\n  start_sec: 120.0, end_sec: 138.0,\n  samples: [\n    {t, x_norm, y_norm, similarity},\n    ...\n  ],\n}'},
  ];
  boxes.forEach((b) => {
    card(s, b.x, b.y, 6.0, 2.4);
    s.addShape(pptx.ShapeType.rect, {
      x: b.x, y: b.y, w: 0.12, h: 2.4, fill: { color: C.accent }, line: { color: C.accent },
    });
    s.addText(b.title, {
      x: b.x + 0.3, y: b.y + 0.15, w: 5.6, h: 0.4,
      fontFace: FH, fontSize: 16, color: C.textHi, bold: true, margin: 0,
    });
    s.addText(b.code, {
      x: b.x + 0.3, y: b.y + 0.6, w: 5.6, h: 1.7,
      fontFace: FM, fontSize: 11, color: C.accent2, valign: 'top', margin: 0,
    });
  });

  footer(s, 12, TOTAL);
}

/* =========== Slide 13 — CLI Usage =========== */
{
  const s = dark();
  setHeader(s, 'CLI 사용법', 'How to run');

  const blocks = [
    {title: '기본 (3개 쇼츠)', code: 'python -m app.cli create_shorts \\\n  --video /path/to/video.mp4 \\\n  --subtitle /path/to/subtitle.srt \\\n  --title "작품명"'},
    {title: 'YouTube URL (자막 자동 다운로드)', code: 'python -m app.cli create_shorts \\\n  --youtube-url https://youtu.be/XXXX \\\n  --title "작품명" --max-shorts 1'},
    {title: '부분 재실행 (캐시 활용)', code: 'python -m app.cli create_shorts \\\n  --video ... --title "..." \\\n  --from-step tts_plan --job-id 작품명_<2hex>'},
    {title: '자막 토글', code: '# TTS 자막 OFF (음성은 그대로)\n--no-tts-subtitles\n\n# 메인 자막도 OFF\n--no-subtitles --no-tts-subtitles'},
  ];
  const w = 6.0, h = 2.3, gx = 0.33, gy = 0.13;
  blocks.forEach((b, i) => {
    const x = 0.5 + (i % 2) * (w + gx);
    const y = 2.0 + Math.floor(i / 2) * (h + gy);
    card(s, x, y, w, h);
    s.addShape(pptx.ShapeType.rect, {
      x, y, w, h: 0.06, fill: { color: C.accent }, line: { color: C.accent },
    });
    s.addText(b.title, {
      x: x + 0.25, y: y + 0.12, w: w - 0.5, h: 0.4,
      fontFace: FH, fontSize: 14, color: C.textHi, bold: true, margin: 0,
    });
    s.addText(b.code, {
      x: x + 0.25, y: y + 0.6, w: w - 0.5, h: h - 0.7,
      fontFace: FM, fontSize: 12, color: C.accent2, valign: 'top', margin: 0,
    });
  });

  footer(s, 13, TOTAL);
}

/* =========== Slide 14 — Conclusion =========== */
{
  const s = dark();
  s.addShape(pptx.ShapeType.rect, {
    x: 0, y: 0, w: 13.33, h: 7.5, fill: { color: C.bgDark }, line: { color: C.bgDark },
  });
  s.addShape(pptx.ShapeType.rect, {
    x: 0.7, y: 0.8, w: 0.15, h: 1.6, fill: { color: C.accent }, line: { color: C.accent },
  });
  s.addText('정리', {
    x: 1.1, y: 0.7, w: 11, h: 0.6,
    fontFace: FB, fontSize: 18, color: C.accent, margin: 0,
  });
  s.addText('Hands-off · 재실행 가능 · 비용 최적화 · 품질 보강', {
    x: 1.1, y: 1.2, w: 11, h: 1.2,
    fontFace: FH, fontSize: 36, color: C.textHi, bold: true, margin: 0,
  });

  card(s, 0.5, 2.8, 6.0, 3.8);
  s.addText('지금 가능한 것', {
    x: 0.8, y: 2.95, w: 5.5, h: 0.4,
    fontFace: FB, fontSize: 14, color: C.accent, bold: true, margin: 0,
  });
  s.addText([
    {text: '• 한국어 드라마 1편 → 60초 쇼츠 1~3개 자동 제작', options: {breakLine: true}},
    {text: '• YouTube URL 입력만으로도 e2e 가능', options: {breakLine: true}},
    {text: '• face_id 사전 인덱스 → 인물 일관성 / 빠른 크롭', options: {breakLine: true}},
    {text: '• TTS cue voice/speed 가변 + 자막 on/off 토글', options: {breakLine: true}},
    {text: '• 16개 체크포인트로 단계별 부분 재실행', options: {}},
  ], {
    x: 0.8, y: 3.4, w: 5.5, h: 3.0,
    fontFace: FB, fontSize: 14, color: C.textHi, valign: 'top', margin: 0,
  });

  card(s, 6.83, 2.8, 6.0, 3.8);
  s.addText('향후 개선', {
    x: 7.13, y: 2.95, w: 5.5, h: 0.4,
    fontFace: FB, fontSize: 14, color: C.accent2, bold: true, margin: 0,
  });
  s.addText([
    {text: '• 다국어 voice 풀 확장 (현재 한국어 3 voice)', options: {breakLine: true}},
    {text: '• character_index 커버리지 향상 (sample / threshold 조정)', options: {breakLine: true}},
    {text: '• chunk-level 메타 다운스트림 활용', options: {breakLine: true}},
    {text: '• 멀티 트랙 자막 (다국어 동시 출력)', options: {breakLine: true}},
    {text: '• 톤 프리셋 사용자 선택 (드라마틱/예능/다큐)', options: {}},
  ], {
    x: 7.13, y: 3.4, w: 5.5, h: 3.0,
    fontFace: FB, fontSize: 14, color: C.textHi, valign: 'top', margin: 0,
  });

  s.addText('Thank you', {
    x: 0.5, y: 6.8, w: 12.3, h: 0.4,
    fontFace: FH, fontSize: 16, color: C.textMuted, align: 'center', italic: true, margin: 0,
  });
}

/* =========== Save =========== */
const out = path.resolve(__dirname, 'AI_Video_Shorts_Creator.pptx');
pptx.writeFile({ fileName: out }).then((f) => {
  console.log('saved:', f);
});
