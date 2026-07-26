clc; clear;

%% =========================
% 1. Input 정보 정의
%% =========================
load('Output_Results/sample_sample_1.mat')

length_raw = double(length_absolute_raw);
ratio_raw  = double(ratio_raw);

layer_lines = [2 3 3 4 4 4 4 4 4 4 4 3 3 2];

% 시작이 300임 ratios_raw(n,2) 지점

lengthN = length(length_raw);
total_lines = sum(layer_lines);
total_length = sum(length_raw);

% 3. 각 시퀀스(Model Index 0~12)별로 모델을 추론/생성
% 1. material_map 생성 (14레이어 x 13시퀀스)
materials = [1 2 3 4 5 100 200 300 400];
material_map = zeros(14, 13);

[~, idx] = sort(ratio_raw(1,:), 'descend');

mat1 = materials(idx(1));   % 시작 재료
mat2 = materials(idx(2));   % 두번째 재료

for i = 1:14

    r1 = ratio_raw(i, materials==mat1);
    r2 = ratio_raw(i, materials==mat2);

    n1 = round(r1 * 13);

    material_map(i,1:n1) = mat1;
    material_map(i,n1+1:13) = mat2;

end

% 2. 데이터 통합 및 압축 (model 생성)
model = cell(13, 3);
for j = 1:13
    seq_col = material_map(:, j); % 현재 시퀀스의 재료 열
    
    % 압축 알고리즘
    compressed = [];
    cur_mat = seq_col(1);
    cur_sum = layer_lines(1);
    
    for i = 2:14
        if seq_col(i) == cur_mat
            cur_sum = cur_sum + layer_lines(i);
        else
            compressed = [compressed; cur_mat, cur_sum];
            cur_mat = seq_col(i);
            cur_sum = layer_lines(i);
        end
    end
    compressed = [compressed; cur_mat, cur_sum];
    
    % model 객체에 할당
    model{j, 1} = j - 1;          % Index
    model{j, 2} = length_raw(j);  % 총 길이
    model{j, 3} = compressed;     % 압축된 재료 데이터 [재료, 라인수]
end

%% =========================
% 기본 설정
% =========================
feedLength_start  = 130;
feedLength_end    = 10;

%% =========================
% Tdef 생성 (material, line수)
% =========================
Tdef = cell(size(model,1),2);
for i = 1:size(model,1)
    idx  = model{i,1};
    comp = model{i,3};
    block = [];
    for j = 1:size(comp,1)
        material = comp(j,1);
        ratio    = comp(j,2);
        nLines   = ratio;   
        if nLines > 0
            block = [block; material nLines];
        end
    end
    Tdef{i,1} = idx;
    Tdef{i,2} = block;
end

%% =========================
% To 생성 (idx, property)
% =========================
To = [];
for i = 1:size(model,1)
    To = [To; model{i,1} model{i,2}];
end
To = flip(repmat([To],1,1));

%% =========================
% Ti 생성
% =========================
Ti = [12, feedLength_start;
    To;
    0, feedLength_end];

%% =========================
% Layer 정의
% =========================
layer_lines = [2 3 3 4 4 4 4 4 4 4 4 3 3 2];
layer_cum   = cumsum(layer_lines);

%% =========================
% PO 자동 생성
% =========================

po = autoPO_ratio_print(model, layer_lines, ratio_raw);

%% =========================
% ====== 함수 정의 ======
% =========================
function PO = autoPO_ratio_print(model, layer_lines, ratios_raw)
    layerN = length(layer_lines);
    [sorted_vals, sorted_indices] = sort(ratios_raw(1, :), 'descend');
    
    % 1. 시작 재료 결정 (Layer 1의 최대 비율 재료)
    mat_map = [1,2,3,4,5,100,200,300,400]; 
    start_idx = sorted_indices(1); % 가장 큰 비율의 인덱스
    other_idx = sorted_indices(2); % 두 번째로 큰 비율의 인덱스
    
    start_mat = mat_map(start_idx);
    other_mat = mat_map(other_idx);
    
    % 2. [데이터 직접 분석] 시작 재료 비율이 증가하기 시작하는 변곡점(Inflection Point) 탐색
    inflection_idx = 1; 
    for L = 2:layerN
        % 시작 재료의 비율이 이전 레이어보다 증가하는 첫 지점을 변곡점으로 잡음
        if ratios_raw(L, start_idx) > ratios_raw(L-1, start_idx)
            inflection_idx = L;
            break; 
        end
    end
    
    % 3. 순차적 구간 할당 (PO 생성)
    % 질문자님 의도: 시작재료 구간 -> 전체 관통 재료 -> 시작재료 복귀 구간
    PO = [
        start_mat, 1, inflection_idx-1;
        other_mat, 1, layerN;
        start_mat, inflection_idx, layerN
    ];
    
    fprintf('\n========== PO RESULT (동적 변곡점 탐지) ==========\n');
    for i = 1:size(PO,1)
        fprintf('%d %d %d\n', PO(i,1), PO(i,2), PO(i,3));
    end
end
%% =========================
% 저장
% =========================
save('Matinfo.mat')