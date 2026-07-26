function po = printingOrderAuto(Ti,Tdef,Sg)

mat = unique(Sg(:,1));
seg = unique(Sg(:,[2 3]),'rows');
po = zeros(1,3);
for i = 1:size(mat,1)
    curMat = mat(i);
    mcl_start = min(Sg(ismember(Sg(:,1),curMat),4));
    mcl_end = max(Sg(ismember(Sg(:,1),curMat),4))+1;
    mcl = mcl_start;

    for j = 1:size(seg,1)
        curSeg = seg(j,:);
        curSg = Sg(ismember(Sg(:,[2 3]),curSeg,'rows'),:);
        for k = 2:size(curSg,1)
            if curSg(k,1) == curMat && curSg(k-1,1) ~= curMat
                mcl_temp = curSg(k-1,4);
                if mcl_start <= mcl_temp
                    mcl = [mcl; curSg(k-1,4)];
                end
            end
        end
    end
    mcl = [mcl; mcl_end];
    mcl = sort(unique(mcl));
    if size(mcl,1) == 2
        mcl = [mcl(1); 8; mcl(2)];
    end

    po_temp = zeros(size(mcl,1)-1,3);
    for h = 1:size(mcl,1)-1
        po_temp(h,:) = [curMat, mcl(h), mcl(h+1)-1];
    end
    po = [po; po_temp];
end
po(1,:) = [];

po_start = po(ismember(po(:,2),1),:);
mo = po_start(:,1);
if size(mo,1) <= size(mat,1)
    mo = [mo; mat(not(ismember(mat,mo)))];
end

po = sortrows(po,3);

po_sort_temp = zeros(1,3);
for k = 1:size(mo,1)
    po_sort = po(ismember(po(:,1),mo(k)),:);
    po_sort_temp = [po_sort_temp; po_sort];
end
po_sort_temp(1,:) = [];

po = sortrows(po_sort_temp,[3 2]);

end
