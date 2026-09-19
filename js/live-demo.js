/* Daily live pitch path; the existing weekly recommendation UI is preserved. */
function viewLiveDemo() {
  app().innerHTML = `
    <div class="page-head"><div class="eyebrow">LIVE · Daytona Execution Plane</div>
      <h1>퇴근 후 6시간, 나에게 맞는 일은?</h1>
      <p>600개 데모 공고를 실제 Daytona에서 분석하고 이동과 귀가 시간을 함께 계획합니다.</p></div>
    <div class="card">
      <div class="step-title">강남에서 출발 → 용산으로 귀가</div>
      <p>토요일 · POS 경험 6개월 · 보건증 · 주간 목표 250,000원</p>
      <div class="actions" style="flex-wrap:wrap;justify-content:flex-start;gap:16px">
        <label>시작 <input id="live-start" type="time" value="14:00"></label>
        <label>귀가 마감 <input id="live-end" type="time" value="20:00"></label>
        <label>선호 업무 <select id="live-preference"><option>매장 정리</option><option>물류</option><option>행사</option></select></label>
        <label><input id="live-negotiate" type="checkbox" checked> 시간 협의 제안 허용</label>
        <button class="btn primary" id="live-run">내 일정 추천받기 →</button>
      </div>
      <p style="font-size:13px;color:var(--muted,#64748b)">합성 공고 데이터 · 이동 시간은 데모 추정치 · 시간 변경 제안은 고용주 확인 필요</p>
    </div>
    <div id="live-output" aria-live="polite" style="margin-top:24px"></div>`;
  $('#live-run').onclick = async () => {
    const button = $('#live-run');
    const output = $('#live-output');
    const payload = {
      start_location: '서울 강남', home_location: '서울 용산', weekday: 'SAT',
      availability: {start: $('#live-start').value, end: $('#live-end').value},
      weekly_income_target: 250000, skills: ['POS 경험 6개월', '보건증'],
      preferred_jobs: [$('#live-preference').value], avoid_jobs: ['설거지', '주방 보조'],
      allow_negotiable_proposals: $('#live-negotiate').checked
    };
    button.disabled = true;
    button.textContent = '일정 분석 중…';
    output.innerHTML = '<div class="card"><h2>Daytona에서 실행 중</h2><p>공고 로딩 → 시간·이동 제약 검사 → 선호도 평가</p></div>';
    const started = performance.now();
    try {
      const response = await fetch('/api/demo/schedule', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || result.error || '요청 실패');
      const meta = result.meta;
      if (meta.runtime_provider !== 'daytona' || !meta.execution_ok) throw new Error('Daytona 실행을 확인할 수 없습니다.');
      const won = value => Number(value).toLocaleString('ko-KR');
      const blocks = result.schedule.map(block => `<div style="display:flex;gap:24px;padding:20px 0;border-bottom:1px solid #e2e8f0">
        <strong style="min-width:110px">${esc(block.start)}–${esc(block.end)}</strong>
        <div><strong>${block.type === 'job' ? '💼 ' + esc(block.title) : '🚇 ' + esc(block.from) + ' → ' + esc(block.to)}</strong>
        ${block.type === 'job' ? `<p>${esc(block.platform)} · ${esc(block.company)} · 시급 ${won(block.hourly_wage)}원</p>` : ''}
        ${block.schedule_status === 'proposed' ? `<p style="color:#92400e">시간 변경 제안: 게시 ${esc(block.published_start)}–${esc(block.published_end)} → 위 시간 · 고용주 확인 필요</p>` : ''}</div></div>`).join('');
      output.innerHTML = `<div class="card">
        <div class="eyebrow">DAYTONA 실행 완료 · ${((performance.now()-started)/1000).toFixed(1)}초</div>
        <h2>${result.schedule.length ? '오늘의 추천 일정' : '현재 조건에 맞는 공고가 없습니다'}</h2>
        <p>${meta.jobs_loaded}개 공고 → 모집 중 ${meta.jobs_recruiting}개 → 조건에 맞는 후보 ${meta.jobs_final_candidates}개</p>
        ${blocks || '<p>게시된 시간을 고정하면 이동과 귀가 조건을 충족하지 못합니다. 시간 협의를 허용하거나 가능한 시간을 넓혀 주세요.</p>'}
        ${result.schedule.length ? `<h2>예상 수입 ${won(result.summary.daily_income)}원</h2><p>주간 목표에 ${result.summary.target_progress_percent}% 기여</p>
        <ul>${result.recommendation.reasons.map(reason => `<li>${esc(reason)}</li>`).join('')}</ul>` : ''}
        <details><summary>실행 근거와 주의사항</summary><p>Sandbox: ${esc(meta.sandbox_id)} · Ranking: ${esc(meta.ranking_provider)}</p>
        <ul>${(meta.warnings || []).map(w => `<li>${esc(w)}</li>`).join('')}</ul>
        <pre style="white-space:pre-wrap">${esc((meta.trace || []).join('\n'))}</pre></details>
        <details><summary>실제 응답 JSON</summary><pre style="white-space:pre-wrap;max-height:300px;overflow:auto">${esc(JSON.stringify(result,null,2))}</pre></details>
      </div>`;
    } catch (error) {
      output.innerHTML = `<div class="card"><h2>실행을 완료하지 못했습니다</h2><p>${esc(error.message)}</p><p>python web_demo.py로 서버를 시작한 뒤 http://127.0.0.1:5191/#/live 에 접속해 주세요.</p></div>`;
    } finally {
      button.disabled = false;
      button.textContent = '조건 바꿔 다시 추천받기 →';
    }
  };
}
