
function po = printingOrder(Ti,Tdef,Sg)


while true
    loop = input('Manually define printing order? (y/n) : ','s'); % 0 to continue 1 to break loop
    if loop == 'y'
        po = input('Write printing order in 3xN array :  ');
        break
    else        
        mat = unique(Sg(:,1));
        seg = unique(Sg(:,[2 3]),'rows');    
        po = zeros(1,3);
        for i = 1:size(mat,1)
            curMat = mat(i);
            mcl_start = min(Sg(ismember(Sg(:,1),curMat),4)); % material change layer(layer num where different material comes), 
                                                       % start with smallest layer num      
            mcl_end = max(Sg(ismember(Sg(:,1),curMat),4))+1;
            mcl = mcl_start;

            for j = 1:size(seg,1)                
                curSeg = seg(j,:);
                curSg = Sg( ismember(Sg(:,[2 3]),curSeg,'rows') ,: );
                for k = 2:size(curSg,1)
                    if curSg(k,1) == curMat && curSg(k-1,1) ~= curMat
                        mcl_temp = curSg(k-1,4);
                        if mcl_start <= mcl_temp
                            mcl = [ mcl; curSg(k-1,4)];
                        end
                    end
                end                
            end
            mcl = [ mcl; mcl_end ];
            mcl = sort(unique(mcl));
            if size(mcl,1) == 2 % means mcl = [1; 15]
                mcl = [mcl(1); 8; mcl(2)];  % split single material segment to prevent nozzle crash
            end
                
%             mcl_temp = mcl(1);
%             for ii = 2 : size(mcl,1)
%                 if mcl(ii) - mcl(ii-1) > 1
%                     mcl_temp = [mcl_temp; mcl(ii)];
%                 end
%             end   
%             mcl = mcl_temp;
            po_temp = zeros(size(mcl,1)-1,3);
            for h = 1:size(mcl,1)-1
                po_temp(h,:) = [curMat, mcl(h), mcl(h+1)-1];
            end
            po = [po; po_temp];            
        end       
        po(1,:) = [];
        
        po_start = po( ismember(po(:,2),1) );
        mo = po_start(:,1);  % material order in 1st layer
        if size(mo,1) <= size(mat,1)
            mo = [mo; mat( not(ismember(mat,mo)) )];
        end        
        
%         mcl_end = unique(po(:,3));
%         mcl_cur = max(po_start(:,3));               
%         mcl_cur_idx_start = find( mcl_end == mcl_cur );
%         
%         po_sort = zeros(1,3);        
%         po_remove = po; 
%             
%         for ii = mcl_cur_idx_start:size(mcl_end)
%             for k = 1:size(mat,1)
%                 idx = find( po_remove(:,1) == mat(k) && po_remove(:,3) <= mcl_cur )
%                 po_sort_temp = po_remove (idx,:);
%                 po_remove(idx,:) = [];
%                 po_sort = [po_sort; po_sort_temp];
%             
% 
%         po_sort(1,:) = [];
        
        
        % sort po according to column in order [2 1 3]
        po = sortrows(po,3); % firstly sort by end layer
        
        % sort by material according to mo
        po_sort_temp = zeros(1,3);
        for k = 1:size(mo,1)
            po_sort = po( ismember( po(:,1),mo(k) ), :);
            po_sort_temp = [po_sort_temp; po_sort];
        end
        po_sort_temp(1,:) = [];
        
        po = sortrows(po_sort_temp,[3 2]);  
        
        break
    end
end

end








