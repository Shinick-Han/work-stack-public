# Task에 Wiki 설명과 원문 근거 연결하기

Work Stack은 과제와 실행 상태를 관리하고, Obsidian은 문서를 보관한다. LLM Wiki의 개념 문서와 원문은 외부 참고 자료로 연결한다. 검색 인덱스는 외부 검색기가 소유하며 Work Stack은 인덱스를 만들거나 문서를 다시 작성하지 않는다.

## 사용 순서

1. Task를 열고 **Resume → View all context → Knowledge sources → Choose folder**에서 로컬 Markdown 폴더를 선택한다.
2. **Related document search**에 궁금한 내용을 입력하고 **Search**를 누른다. Task를 선택하는 것만으로 검색이나 모델 호출을 시작하지 않는다.
3. 검색 결과의 문서 범위·인덱스 시점과 원문 발췌를 확인한다. **Use this result**로 경로와 줄 범위를 채운다.
4. **Link reason**에 이 Task에 필요한 이유를 적고 **Link**한다. 원문이 바뀌면 다시 확인해야 한다.
5. Wiki의 개념 설명도 필요하면 기존 **Document relative path → Preview → Link**로 추가한다. 검색되지 않는 문서도 직접 연결할 수 있다.
6. Task context 화면에서 이번 재개에 필요한 문서만 선택하고 **Prepare resume brief → Copy resume brief**를 누른다.
7. 복사한 문서를 원하는 agent 세션에 붙여 넣는다. 저장된 Task의 내용·상태·revision과 선택한 참고 자료, 연결 이유가 함께 전달된다. 자동으로 agent를 실행하거나 메시지를 보내지는 않는다.

기존 **Copy JSON / Download JSON**도 유지한다. 변경된 원문에 대한 확인, 최대 8개 문서 선택과 32 KiB 한도는 재개용 문서에도 적용한다. 저장하지 않은 Task 편집 내용을 저장된 상태로 표시하지 않는다.

## 검색과 근거의 경계

- 검색기는 상대 경로·줄 범위·기대한 내용 해시만 제안한다. 실제 발췌는 데스크톱 호스트가 선택한 vault에서 다시 읽는다.
- 변경·삭제·vault 밖의 후보는 현재 근거로 표시하지 않는다. 검색 실패 시 기존 수동 문서 연결은 계속 사용할 수 있다.
- 연결 정보는 이 기기에 저장한다. workspace 동기화나 일반 `agent context`에 자동 포함되지 않는다. 다른 기기에 옮기려면 vault와 로컬 연결 설정도 별도로 준비해야 한다.
- Wiki의 설명은 정리된 해석이며 원문과 구분해서 읽는다. 둘을 함께 연결하면 설명을 이해하면서 원래 결정의 근거도 확인할 수 있다.

## 검색기 설정

검색기는 별도 설정한다. 설정한 corpus의 일부만 색인되어 있을 수 있으므로 검색 결과의 실제 문서 수와 인덱스 시점을 확인한다. 폴더를 연결했다고 전체 문서나 Wiki가 자동 색인되는 것은 아니다.

사용하는 embedding/rerank/생성 모델과 API 비용은 외부 검색기 설정에 따른다. Work Stack 자체가 모델이나 API 키를 제공하지 않는다. 외부 검색기의 결과를 실제 로컬 문서와 다시 대조하며, 이 기능이 전체 재인덱싱을 시작하지는 않는다.

검색기 설정은 데스크톱 StateRoot의 `knowledge/search-provider.json`에 둔다. 실행 파일과 고정 인수를 지정하는 신뢰된 로컬 설정이며 UI가 명령을 입력받지 않는다. API 키는 이 파일이나 Work Stack 저장소에 복사하지 않고 외부 검색기의 기존 인증 설정을 사용한다. 프로토콜과 제한은 [KNOWLEDGE-SEARCH.md](KNOWLEDGE-SEARCH.md)에 설명한다.

## 검증 범위

구현 완료 여부는 별도 검증 기록으로 확인한다. 브라우저에서 흉내 낸 native transport, 실제 WebView2, 설치된 EXE, 개인 vault 연결은 서로 다른 검증이다. 회사 SSH/NFS 환경에서 이 개인 검색기를 실행했다는 의미도 아니다.
